# QNX Neutrino RTOS & POSIX Makefile for Problem #48 Digital Twin Engine
# Supports Linux/GCC, QNX x86_64, QNX ARM64 (RPi4), and QNX ARM32 (RPi4)

CC ?= gcc
CFLAGS ?= -O2 -Wall -I./src
LDFLAGS ?= -lpthread -lrt

# Auto-detect QNX Host environment
ifneq ($(QNX_HOST),)
    CC = qcc
    CFLAGS = -Vgcc_ntoaarch64le -O2 -Wall -I./src
endif

TARGET = twin_engine
SRC = src/qnx_twin_engine.c

all: $(TARGET)

$(TARGET): $(SRC)
	$(CC) $(CFLAGS) $(SRC) -o $(TARGET) $(LDFLAGS)

# QNX Cross-Compilation Targets for Raspberry Pi 4 B
qnx-x86:
	qcc -Vgcc_ntox86_64 -O2 -Wall -I./src $(SRC) -o $(TARGET) -lrt

qnx-arm64:
	qcc -Vgcc_ntoaarch64le -O2 -Wall -I./src $(SRC) -o $(TARGET) -lrt

qnx-arm32:
	qcc -Vgcc_ntoarmv7le -O2 -Wall -I./src $(SRC) -o $(TARGET) -lrt

clean:
	rm -f $(TARGET) *.o

.PHONY: all qnx-x86 qnx-arm64 qnx-arm32 clean

