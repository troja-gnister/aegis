CC      = gcc
CFLAGS  = -Wall -Wextra -Werror -std=c11 -Iinclude -Ivendor
LDFLAGS =

# Library sources (everything in src/ except cli/)
LIB_SRCS  = $(wildcard src/*.c) $(wildcard src/modules/*.c)
LIB_OBJS  = $(LIB_SRCS:.c=.o)
VENDOR_SRCS = vendor/tomlc99/toml.c
VENDOR_OBJS = $(VENDOR_SRCS:.c=.o)

LIB = libaegis.a
BIN = aegis

# Test binaries: tests/test_foo.c -> tests/test_foo
TEST_SRCS = $(wildcard tests/test_*.c)
TEST_BINS = $(TEST_SRCS:.c=)

.PHONY: all clean test install

all: $(BIN)

$(LIB): $(LIB_OBJS) $(VENDOR_OBJS)
	ar rcs $@ $^

$(BIN): src/cli/main.o $(LIB)
	$(CC) $(CFLAGS) -o $@ $< -L. -laegis $(LDFLAGS)

src/cli/main.o: src/cli/main.c
	$(CC) $(CFLAGS) -c -o $@ $<

%.o: %.c
	$(CC) $(CFLAGS) -c -o $@ $<

test: $(LIB) $(TEST_BINS)
	@failed=0; \
	for t in $(TEST_BINS); do \
		echo "=== $$t ==="; \
		./$$t || failed=1; \
	done; \
	if [ $$failed -eq 1 ]; then echo "\nSome tests failed"; exit 1; \
	else echo "\nAll tests passed"; fi

tests/test_%: tests/test_%.c $(LIB)
	$(CC) $(CFLAGS) -o $@ $< -L. -laegis $(LDFLAGS)

clean:
	rm -f $(LIB_OBJS) $(VENDOR_OBJS) $(LIB) $(BIN) src/cli/main.o $(TEST_BINS)

install: $(BIN)
	install -Dm755 $(BIN) $(DESTDIR)/usr/local/bin/$(BIN)
