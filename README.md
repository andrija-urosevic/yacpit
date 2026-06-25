# YACPIT - Yet Another Competitive Programming Interface Tool

This folder contains the small test harness and utilities used for running and
verifying student solutions for the exercise set. The main tool is `yacpit` —
"Yet Another Competitive Programming Interface Tool" — implemented as a
Python script in this directory.

## Prerequisites

- Python 3.8+ (for running the `yacpit.py` script and HTML generation)
- gcc (for compiling C solutions used by the test runner)
- make (optional, if task directories include a `Makefile`)

On Debian/Ubuntu you can install the essentials with:

```bash
sudo apt update
sudo apt install python3 python3-venv python3-pip build-essential make
```

On Arch/Manjaro you can install equivalent packages with pacman:

```bash
sudo pacman -Syu
sudo pacman -S --needed python python-pip base-devel
```

## Installing yacpit (optional)

This repository contains a local script. You can run it directly without
installation:

```bash
cd Vezbe/tester
python3 yacpit.py --help
```

### Using a virtual environment (recommended)

It's a good practice to use a Python virtual environment to isolate dependencies.
From the `Vezbe/tester` directory run:

```bash
# create the virtual environment in a `.my_venv` directory
python3 -m venv .my_venv

# activate the my_venv (Linux/macOS)
source .my_venv/bin/activate
```

If you prefer to run `yacpit` as a command after activating the venv, `yacpit` you can install it into the venv:

```bash
cd Vezbe/tester
pip install .
```

## Common usage

Initiate new task directory named `08_test_task` with template files:

```bash
yacpit init-task 08 test_task
```

Run tests for a single task (example: task number `08`):

```bash
yacpit test-task 08
```

Run tests for all tasks in the current environment:

```bash
yacpit test-all
```

Generate an HTML preview for a single task:

```bash
yacpit html-task 08
```

Generate HTML for all tasks:

```bash
yacpit html-all
```

Run the script with `--help` for a complete list of available commands and
options:

```bash
yacpit --help
```
