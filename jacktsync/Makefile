CFLAGS+= -Wall -O2

all: jacktsync

jacktsync: jacktsync.o
	gcc -o $@ $< -ljack -lm

clean:
	rm -f jacktsync *.o
