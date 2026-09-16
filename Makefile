# QNX Neutrino RTOS & POSIX Makefile for Problem #48 Digital Twin Engine
# Supports Linux/GCC, QNX x86_64, QNX ARM64 (RPi4), and QNX Clang

CC ?= clang
CFLAGS ?= -O2 -Wall -I./src
LDFLAGS ?= -lsocket -lpthread -lrt

TARGET = qnx_twin_engine
SRC = src/qnx_twin_engine.c

all: $(TARGET)

$(TARGET): $(SRC)
	$(CC) $(CFLAGS) $(SRC) -o $(TARGET) $(LDFLAGS)

qnx-clang:
	clang $(CFLAGS) $(SRC) -o $(TARGET) -lsocket -lpthread -lrt

qnx-gcc:
	gcc $(CFLAGS) $(SRC) -o $(TARGET) -lsocket -lpthread -lrt

clean:
	rm -f $(TARGET) *.o

.PHONY: all qnx-clang qnx-gcc clean
