#!/usr/bin/env python3

#import OpenGL.GL as gl
import time, math, socket, threading, fcntl, struct, signal, random, sys, traceback
import mpv

PORT=4444
SIOCGSTAMP=0x8906
IS_RPI = b"BCM2709" in open("/proc/cpuinfo", "rb").read()

# Based on timefilter.c from ffmpeg
class DLL(object):
    def __init__(self, period, bandwidth):
        o = 2 * math.pi * bandwidth * period

        self.fb2 = 1 - math.exp(-(2**0.5) * o)
        self.fb3 = (1 - math.exp(-o * o)) / period
        self.period = 1.0
        self.count = 0
        self.lock = threading.Lock()

    def update(self, timestamp, value):
        with self.lock:
            self.count += 1
            if self.count == 1:
                self.cycle_time = timestamp
                self.cycle_value = value
            else:
                delta = value - self.cycle_value
                self.cycle_time += self.period * delta
                self.cycle_value = value

                loop_error = timestamp - self.cycle_time
                self.cycle_time += max(self.fb2, 1.0 / self.count) * loop_error
                self.period += self.fb3 * loop_error
                #print("ts %.04f v %.04f period %.04f err %.04f ct %.04f cv %.04f" % (
                #timestamp, value, self.period, loop_error, self.cycle_time, self.cycle_value))
    def evaluate(self, timestamp):
        with self.lock:
            return self.cycle_value + (timestamp - self.cycle_time) / self.period

class Filter(object):
    def __init__(self, alpha):
        self.alpha = alpha
        self.v = None

    def update(self, v):
        if self.v is None:
            self.v = v
        else:
            self.v = v * self.alpha + self.v * (1 - self.alpha)

    def reset(self):
        self.v = None

    @property
    def value(self):
        return self.v

class ListenerThread(threading.Thread):
    BW = 1
    TIMEVAL = "lI"
    TIMEVAL_SZ = struct.calcsize(TIMEVAL)
    def __init__(self):
        super().__init__()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("0.0.0.0", PORT))
        self.period = None
        self.dll = None
        self.last = None
        self.last_t = 0
        self.active = True
    def run(self):
        while self.active:
            try:
                data, addr = self.sock.recvfrom(1024)
            except OSError:
                continue
            buf = fcntl.ioctl(self.sock, SIOCGSTAMP, "\0" * self.TIMEVAL_SZ)
            sec, usec = struct.unpack(self.TIMEVAL, buf)
            timestamp = sec + usec / 1000000.0

            #print("@%.06f > %s" % (timestamp, data))

            args = dict(tuple(i.split("=", 1)) for i in data.decode("ascii").split())

            t = int(args["f"]) / int(args["r"])
            self.last = args
            self.last_t = t

            if args["state"] != "rolling" or self.period != int(args["p"]):
                self.dll = None
                self.period = int(args["p"])

            if args["state"] == "rolling":
                if self.dll is None:
                    self.dll = DLL(self.period / 1000000.0, self.BW)

                self.dll.update(timestamp, t)

    def now(self, t=None):
        if t is None:
            t = time.time()
        if self.dll is None:
            return self.last_t
        else:
            return self.dll.evaluate(t)

    @property
    def rolling(self):
        return self.last and self.last["state"] == "rolling"

class Player(object):
    JUMP_THRESHOLD = 2.0
    SPEED_THRESHOLD = 0.01
    SPEED_FACTOR = 1
    SPEED_UPDATE_RATE = 0.25
    def __init__(self, playlist, display=None):
        self.playlist = []
        self.cur = None
        self.pause = True
        self.speed = 1.0

        for line in open(playlist):
            line = line.strip().split("#")[0]
            if not line:
                continue
            off, preroll, filename = line.strip().split(None, 2)
            off = float(off)
            preroll = float(preroll)
            self.playlist.append((off, preroll, filename))

        self.display = display
        self.mpv = mpv.Context()
        self.mpv.initialize()
        self.mpv.set_property("audio-file-auto", "no")
        #self.mpv.set_property("terminal", True)
        #self.mpv.set_property("quiet", True)
        self.mpv.set_property("audio", "no")
        self.mpv.set_property("keep-open", "yes")
        if IS_RPI:
            self.mpv.set_property("fs", True)
        self.mpv.set_property("input-default-bindings", True)
        self.mpv.set_property("input-vo-keyboard", True)

        if display:
            def gpa(name):
                return display.get_proc_address(name)

            self.gl = self.mpv.opengl_cb_api()
            self.gl.init_gl(None, gpa)
            self.mpv.set_property("vo", "opengl-cb")
        else:
            self.gl = None
            self.mpv.set_property("vo", "rpi" if IS_RPI else "opengl")

    def get_entry(self, ts):
        for i, (off, preroll, filename) in enumerate(self.playlist):
            if (off - preroll) > ts:
                return max(i - 1, 0)
        else:
            return len(self.playlist) - 1

    def load_cur(self, ts):
        need = self.get_entry(ts)
        if self.cur is None or need != self.cur:
            self.cur = need
            self.set_pause(True)
            self.load_file(self.playlist[need][2])

    def seek(self, ts):
        self.load_cur(ts)

        off, preroll, filename = self.playlist[self.cur]
        pts = ts - off
        print("Seek-video: %.03f" % pts)
        if pts > self.duration:
            pts = self.duration
        self.mpv.set_property("time-pos", pts)

    def load_file(self, filename):
        self.pause = False
        self.set_pause(True)
        self.set_speed(1.0)
        print("Load: %s" % filename)
        self.mpv.command('loadfile', filename)
        self._wait_ev(mpv.Events.file_loaded)
        self.duration = self._getprop("duration")

        w = self._getprop("video-params/w")
        h = self._getprop("video-params/h")
        aspect = self._getprop("video-params/aspect")
        print("Loaded %s %dx%d aspect:%f duration:%f" % (filename, w, h, aspect, self.duration))

    def run(self, listener):
        last = None
        last_speed_update = 0
        diff_filt = Filter(0.05)
        self.alive = True
        while self.alive:
            time.sleep(0.01)
            self.poll()

            now = listener.now()
            if not listener.rolling:
                self.set_pause(True)
                if last != now:
                    print("Seek: %.03f" % now)
                    self.seek(now)
                last = now
                continue

            self.load_cur(now)
            off, preroll, filename = self.playlist[self.cur]
            try:
                pts = self.mpv.get_property("time-pos")
                diff = (now - off) - pts
                diff_filt.update(diff)
                fdiff = diff_filt.value
                if (now - off) > self.duration:
                    print("PTS:%.03f NOW:%.03f EOF" % (pts, now - off))
                    time.sleep(0.1)
                    self.set_pause(True)
                    continue
                if self.pause and fdiff >= 0 and fdiff < self.JUMP_THRESHOLD:
                    self.set_pause(False)
                    diff_filt.reset()
                if self.pause and (now - off) < 0:
                    print("PTS:%.03f NOW:%.03f PREROLL" % (pts, now - off))
                    continue
                print("PTS:%.03f NOW:%.03f DIFF:%.03f / %.03f" % (pts, now - off, diff, fdiff))
                self.set_pause(False)
                if abs(fdiff) < self.SPEED_THRESHOLD:
                    self.set_speed(1.0)
                    last_speed_update = time.time()
                elif abs(fdiff) < self.JUMP_THRESHOLD:
                    if time.time() > (last_speed_update + self.SPEED_UPDATE_RATE):
                        last_speed_update = time.time()
                        if fdiff < 0:
                            d = (fdiff + self.SPEED_THRESHOLD) * self.SPEED_FACTOR
                            speed = 1.0 + -d
                        else:
                            d = (fdiff - self.SPEED_THRESHOLD) * self.SPEED_FACTOR
                            speed = 1.0 / (1 + d)
                        print("speed: %.03f" % speed)
                        self.set_speed(speed)
                else:
                    self.seek(now)
                    diff_filt.reset()
                time.sleep(0.01)
            except mpv.MPVError as e:
                print("EOF?", now - off)
                print(e)
                pass

    def _getprop(self, p):
        for i in range(10):
            try:
                return self.mpv.get_property(p)
            except mpv.MPVError: # Wait until available
                self.poll()
                time.sleep(0.1)
        else:
            raise Exception("Timed out getting property %s" % p)

    def _wait_ev(self, ev_id):
        while True:
            for i in self.poll():
                if i.id == ev_id:
                    return

    def set_pause(self, pause):
        if self.pause != pause:
            self.mpv.set_property("pause", pause, async=True)
            self.pause = pause

    def set_speed(self, speed):
        if self.speed != speed:
            self.mpv.set_property("speed", 1.0/speed)
            self.speed = speed

    def poll(self):
        repoll = set()
        evs = []
        while True:
            ev = self.mpv.wait_event(0)
            if ev.id == mpv.Events.none:
                break
            if (ev.id == mpv.Events.get_property_reply
                and ev.data.name in self.poll_props):
                self.poll_props[ev.data.name] = ev.data.data
                repoll.add(ev.data.name)
            elif ev.id == mpv.Events.end_file:
                print("event: %s" % ev.name)
                self.eof = True
            else:
                print("event: %s" % ev.name)
                evs.append(ev)
                if ev.name == "shutdown":
                    self.alive = False
        for i in repoll:
            self.mpv.get_property_async(i)
        return evs

    def flip(self):
        self.gl.report_flip(0)

    def draw(self):
        self.gl.draw(0, self.display.win_width, -self.display.win_height)

    def draw_fade(self, songtime):
        brightness = 1
        if songtime > (self.duration - self.fade_out) and self.fade_out:
            brightness *= max(0, min(1, (self.duration - songtime) / self.fade_out))
        if self.offset < 0:
            songtime += self.offset
        if songtime < self.fade_in and self.fade_in:
            brightness *= max(0, min(1, songtime / self.fade_in))
        if brightness != 1:
            gl.glEnable(gl.GL_BLEND)
            gl.glBlendFunc(gl.GL_SRC_ALPHA, gl.GL_ONE_MINUS_SRC_ALPHA);
            gl.glColor4f(0, 0, 0, 1 - brightness)
            gl.glBegin(gl.GL_TRIANGLE_FAN)
            gl.glVertex2f(0, 0)
            gl.glVertex2f(1, 0)
            gl.glVertex2f(1, 1)
            gl.glVertex2f(0, 1)
            gl.glEnd()
        return brightness

    def eof_reached(self):
        t = self.mpv.get_property("time-pos") or 0
        return self.eof or t > self.duration

    def stop(self):
        self.mpv.command('stop')
        self.song = None

    def shutdown(self):
        if self.gl:
            self.gl.uninit_gl()
        self.mpv.shutdown()

if __name__ == "__main__":
    def nop(*args):
        print("SIGUSR1")

    signal.signal(signal.SIGUSR1, nop)
    listener = ListenerThread()
    listener.start()

    try:
        player = Player(sys.argv[1])
        player.run(listener)
    except Exception as e:
        traceback.print_exc()

    player = None
    print("Shutting down...")
    listener.active = False
    try:
        listener.sock.close()
        print("Socket closed...")
    except Exception as e:
        print(e)
    signal.pthread_kill(listener.ident, signal.SIGUSR1)
    listener.join()

