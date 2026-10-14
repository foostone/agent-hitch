"""Opt-in stacks for stuck fixture workers; never imported by the installed package."""
import faulthandler
import os

if os.environ.get("HITCH_INFERENCE_TEST_TRACEBACK") == "1":
    faulthandler.dump_traceback_later(10, repeat=True)
