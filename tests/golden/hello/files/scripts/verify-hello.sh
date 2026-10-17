#!/bin/bash
test -f hello.txt || { echo "hello.txt is missing"; exit 1; }
test "$(cat hello.txt)" = "hello, queue" || { echo "hello.txt does not contain exactly: hello, queue"; exit 1; }
echo "hello.txt is right"
