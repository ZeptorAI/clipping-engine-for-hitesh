#!/usr/bin/env python3
"""
Blocks until a NEW pending job appears, prints its job id, and exits.
Claude runs this in the background; when it exits, Claude processes the job
and re-arms the watcher. Prints only ASCII.
"""

import json
import os
import sys
import time

APP_DIR = os.path.dirname(os.path.abspath(__file__))
JOBS_DIR = os.path.join(APP_DIR, "jobs")


def pending_jobs():
    out = []
    if not os.path.isdir(JOBS_DIR):
        return out
    for name in sorted(os.listdir(JOBS_DIR)):
        sp = os.path.join(JOBS_DIR, name, "status.json")
        if os.path.isfile(sp):
            try:
                with open(sp, encoding="utf-8") as f:
                    if json.load(f).get("status") == "pending":
                        out.append(name)
            except Exception:
                pass
    return out


if __name__ == "__main__":
    print("watch: waiting for a new job…", flush=True)
    while True:
        p = pending_jobs()
        if p:
            print("PENDING_JOB " + p[0], flush=True)
            sys.exit(0)
        time.sleep(2)
