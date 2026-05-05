#!/bin/bash

#
# SPDX-License-Identifier: MIT
# Copyright (c) 2023-2024 Kilian Lackhove
#

# This is an extended Bash script with some common syntax elements including a case statement. It was created by ChatGPT
# with minor manual modifications

# Variable assignment
variable="Hello, World!"

# Printing variables
echo $variable

# Conditionals
if [ "$variable" == "Hello, World!" ]; then
  echo "Variable is set to 'Hello, World!'"
else
  echo "Variable is not set to 'Hello, World!'"
fi

# Loops
for i in {1..5}; do
  echo "Iteration $i"
done

# Functions
function say_hello() {
  echo "Hello from a function!"
}

say_hello

# Command substitution
os=$(uname)
echo "Current OS is: $os"

# Arithmetic operations
result=$((5 + 3))
echo "5 + 3 = $result"

# File operations
touch example_file.txt
echo "This is a sample file." > example_file.txt
cat example_file.txt
rm -f example_file.txt

# Case statement
fruit="banana"
case $fruit in
  "apple")
    echo "You selected an apple."
    ;;
  "banana")
    echo "You selected a banana."
    ;;
  "orange")
    echo "You selected an orange."
    ;;
  *)
    echo "Unknown fruit."
    ;;
esac

# branch coverage example
selected=""
if [[ $fruit == "banana" ]]; then
    selected="banana"
fi
echo $selected


echo multi \
    line
echo \
    multi \
    line \
    again
