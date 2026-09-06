#!/usr/bin/env python3
"""
Conform list for the tightening engine.

Writes the human/editor-facing record of a cut: every surviving segment with its
output timecode next to its source timecode, so an editor can relink any line
back to the original take. Flagged spans from review.py go at the top, because
those are the only lines anyone needs to actually check.
"""

import io


def tc(t):
    """HH:MM:SS.mmm"""
    return "%02d:%02d:%06.3f" % (int(t // 3600), int(t % 3600 // 60), t % 60)


def write_editlog(path, source_name, stats, lines, flags=None):
    with io.open(path, "w", encoding="utf-8") as f:
        f.write("FAST-CUT CONFORM LIST\n")
        f.write("=" * 78 + "\n")
        f.write("Source   : %s (%.2fs)\n" % (source_name, stats["source_sec"]))
        f.write("Output   : %.2fs (%dm%04.1fs) from %d segments\n"
                % (stats["output_sec"], int(stats["output_sec"] // 60),
                   stats["output_sec"] % 60, stats["segments"]))
        f.write("Removed  : %d retake/false-start cuts + measured silence "
                "(-%s dB, adaptive)\n"
                % (stats["retake_cuts"], stats["silence_db"]))
        f.write("Speech   : %.1fs before -> %.1fs after (%.1f%% kept)\n"
                % (stats["speech_before_sec"], stats["speech_after_sec"],
                   stats["speech_kept_pct"]))
        f.write("=" * 78 + "\n\n")

        if flags:
            f.write("SPANS TO CHECK (%d)\n" % len(flags))
            f.write("-" * 78 + "\n")
            for fl in flags:
                f.write("[%-6s] line %d  at %s\n" % (fl["severity"].upper(),
                                                     fl["line"], tc(fl["start"])))
                f.write("           %s\n" % (fl["text"][:70] or "(no words)"))
                f.write("           issue: %s\n" % fl["issue"])
                if fl.get("fix"):
                    f.write("           fix:   %s\n" % fl["fix"])
                f.write("\n")
        else:
            f.write("SPANS TO CHECK: none - the review pass found no damage.\n\n")

        f.write("%4s  %13s %13s  %13s %13s  %6s  TEXT\n"
                % ("#", "OUT IN", "OUT OUT", "SRC IN", "SRC OUT", "DUR"))
        f.write("-" * 78 + "\n")
        flagged = {fl["line"] for fl in (flags or [])}
        t = 0.0
        for l in lines:
            d = l["end"] - l["start"]
            mark = " <<" if l["line"] in flagged else ""
            f.write("%4d  %13s %13s  %13s %13s  %6.2f  %s%s\n"
                    % (l["line"], tc(t), tc(t + d), tc(l["start"]),
                       tc(l["end"]), d, l["text"], mark))
            t += d
