"""Tests of the bash completion in completions/vm-boot-order-2.py."""
import contextlib
import importlib.machinery
import importlib.util
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPT = os.path.join(ROOT, "vm-boot-order-2.py")
COMPLETION = os.path.join(ROOT, "completions", "vm-boot-order-2.py")
COMMANDS = ("start", "stop", "restart", "status")


def load_script():
    """The script as a module; a stand-in for libvirt if it is not installed, the parser does not need it."""
    try:
        import libvirt  # noqa: F401
    except ImportError:
        sys.modules["libvirt"] = types.ModuleType("libvirt")
    loader = importlib.machinery.SourceFileLoader("vm_boot_order_2", SCRIPT)
    spec = importlib.util.spec_from_loader("vm_boot_order_2", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


vbo = load_script()


def complete(*words, cwd=None):
    """The candidates bash offers for the last of words, which may be empty."""
    words = ("vm-boot-order-2.py",) + words
    script = 'source "$1"; shift; COMP_WORDS=("$@"); COMP_CWORD=$(($# - 1)); ' \
             '$(complete -p "$1" | sed -E "s/.*-F ([^ ]+) .*/\\1/"); printf "%s\\n" "${COMPREPLY[@]}"'
    result = subprocess.run(["bash", "-c", script, "bash", COMPLETION] + list(words),
                            capture_output=True, text=True, check=True, cwd=cwd)
    return [line for line in result.stdout.split("\n") if line]


def help_words(*args):
    """The option strings and commands --help lists.

    Before Python 3.13 argparse repeats the metavar after every option string
    ("-H HOST, --host HOST"), since then only after the last ("-H, --host HOST").
    An option line starts with the invocation, followed by the description after
    two or more spaces or, if the invocation is long, on the next line.
    """
    output = io.StringIO()
    with contextlib.redirect_stdout(output), contextlib.suppress(SystemExit):
        vbo.main(list(args) + ["--help"])
    words = set()
    for line in output.getvalue().split("\n"):
        match = re.match(r"  (-\S.*?)(?:\s{2,}|$)", line)
        if match:
            words.update(re.findall(r"(?:^|, )(-{1,2}[^\s,=\[]+)", match.group(1)))
        match = re.match(r"\s{2}\{([a-z,]+)\}$", line)
        if match:
            words.update(match.group(1).split(","))
    return words


@unittest.skipUnless(shutil.which("bash"), "bash is not installed")
class CompletionTests(unittest.TestCase):
    def test_syntax(self):
        subprocess.run(["bash", "-n", COMPLETION], check=True)

    def test_offers_exactly_the_commands_and_options_of_help(self):
        self.assertEqual(set(complete("")), help_words())
        self.assertTrue(set(COMMANDS) <= help_words())

    def test_offers_exactly_the_options_of_the_command_help(self):
        for command in COMMANDS:
            self.assertEqual(set(complete(command, "")), help_words(command), command)

    def test_command_prefix(self):
        self.assertEqual(complete("st"), ["start", "stop", "status"])
        self.assertEqual(complete("-v", "re"), ["restart"])

    def test_command_after_config(self):
        self.assertEqual(complete("-c", "my.conf", "sta"), ["start", "status"])
        self.assertEqual(complete("--config", "=", "my.conf", "-v", "rest"), ["restart"])
        self.assertEqual(complete("-c", "stop", "st"), ["start", "stop", "status"])
        self.assertEqual(complete("-c", "my.conf", "stop", "--"), ["--help", "--stop-all"])

    def test_config_file(self):
        with tempfile.TemporaryDirectory() as directory:
            open(os.path.join(directory, "vms.conf"), "w").close()
            self.assertEqual(complete("-c", "vm", cwd=directory), ["vms.conf"])
            self.assertEqual(complete("--config", "=", "vm", cwd=directory), ["vms.conf"])

    def test_unknown_command(self):
        self.assertEqual(complete("bogus", ""), [])


if __name__ == "__main__":
    unittest.main()
