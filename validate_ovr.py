# -*- coding: utf-8 -*-
"""For each override: show the semantic intervals it DROPS and the text its
keeps RESTORE, so unintended losses at the window edges are visible."""
import json, os, sys, io
sp = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, sp)
import tighten as T

tj, ovrf = sys.argv[1], sys.argv[2]
w = T.load_words(tj)
d, _ = T.detect_repeats(w)
sem = T.keep_intervals(w, d)
ovr = json.load(open(ovrf, encoding="utf-8"))

def txt(a, b, only_kept=True):
    return " ".join(x["raw"].strip() for i, x in enumerate(w)
                    if x["s"] < b and x["e"] > a and (not only_kept or not d[i]))

out = io.open(os.path.join(sp, "ovr_check.txt"), "w", encoding="utf-8")
for o in ovr:
    a, b = o["win"]
    out.write("\n" + "=" * 70 + "\n%s\nWINDOW [%.2f - %.2f]\n" % (o.get("_n", ""), a, b))
    for s, e in sem:
        if s < b and a < e:
            partial = "  <-- PARTIAL (window cuts this interval)" if (s < a or e > b) else ""
            out.write("  DROP [%7.2f-%7.2f]%s\n        %s\n" % (s, e, partial, txt(s, e)))
    for s, e in o.get("keeps", []):
        out.write("  KEEP [%7.2f-%7.2f]\n        %s\n" % (s, e, txt(s, e, only_kept=False)))
    if not o.get("keeps"):
        out.write("  KEEP (nothing - window fully removed)\n")
out.close()
print("wrote ovr_check.txt")
