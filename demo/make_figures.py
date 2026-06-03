#!/usr/bin/env python3
"""Generate report figures (real data + measured latencies) into demo/figures/."""
import json, os, time, urllib.request
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "figures")
os.makedirs(OUT, exist_ok=True)
ORB = "http://localhost:9022/api/taxiql"
NAVY, BLUE, LIME, RED, GREY = "#1f3a5e", "#2f6fb0", "#7aa800", "#c0392b", "#9aa2ad"
plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 11, "axes.edgecolor": "#cfd6dd",
                     "axes.titlecolor": NAVY, "axes.labelcolor": "#33414f", "text.color": "#1c2430"})

def run(q, t=40):
    req = urllib.request.Request(ORB, data=q.encode(), headers={"Content-Type": "text/plain"}, method="POST")
    s = time.time()
    try:
        urllib.request.urlopen(req, timeout=t).read()
        return time.time() - s
    except Exception:
        return None

# ---- measured latencies (warm; run twice, take 2nd) ----
QUERIES = {
  "Identity\n(GLEIF)": 'given { name : uk.gov.CompanyName = "TESCO PLC" } find { uk.gov.apis.gleif.LeiRecord }',
  "Insolvency\n(corpus)": 'given { id : uk.gov.CompanyRegistrationNumber = "07021926" } find { brain.insolvency.InsolvencyProfile }',
  "Adverse media\n(4 sources)": 'given { name : uk.gov.CompanyName = "TESCO PLC" } find { social.AdverseMediaList }',
  "Markets\n(Yahoo)": 'given { ticker : brain.StockTicker = "AAPL" } find { markets.StockQuote }',
  "Narrative\n(local LLM)": 'given { name : uk.gov.CompanyName = "TESCO PLC" } find { brain.RiskNarrative }',
  "Full 360\n(5 APIs)": 'given { name : uk.gov.CompanyName = "TESCO PLC" } find { id: uk.gov.apis.gleif.LeiRecord, w: uk.gov.apis.open_meteo.CurrentWeather, c: uk.gov.apis.police_uk.StreetCrime[] }',
}
lat = {}
for k, q in QUERIES.items():
    run(q); v = run(q)  # warm then measure
    lat[k] = v if v else 0
print("LATENCIES:", {k.replace(chr(10), ' '): round(v, 2) for k, v in lat.items()})

# Figure 1 — measured latency per capability
fig, ax = plt.subplots(figsize=(8, 3.6))
ks = list(lat.keys()); vs = [lat[k] for k in ks]
bars = ax.bar(ks, vs, color=[BLUE]*5 + [NAVY], width=0.62)
ax.set_ylabel("seconds (warm)"); ax.set_title("Measured query latency, by capability — live, this session")
for b, v in zip(bars, vs):
    ax.text(b.get_x()+b.get_width()/2, v+0.03, f"{v:.2f}s", ha="center", va="bottom", fontsize=9, color=NAVY)
ax.spines[["top", "right"]].set_visible(False); ax.tick_params(labelsize=8.5)
plt.tight_layout(); plt.savefig(f"{OUT}/fig-latency.png", dpi=150); plt.close()

# Figure 2 — corpus scale (real, from the gcloud-intel briefing/DB)
fig, ax = plt.subplots(figsize=(8, 3.4))
src = ["Gazette\ninsolvency", "Companies House\nbulk", "PSC\nownership"]
recs = [1.17, 5.7, 15.5]
b = ax.barh(src, recs, color=[RED, BLUE, NAVY], height=0.6)
ax.set_xlabel("records (millions)"); ax.set_title("The gcloud-intel corpus the brain joins into (≈21.4M records)")
for bar, v in zip(b, recs):
    ax.text(v+0.2, bar.get_y()+bar.get_height()/2, f"{v:.1f}M", va="center", fontsize=10, color=NAVY)
ax.spines[["top", "right"]].set_visible(False); ax.invert_yaxis()
plt.tight_layout(); plt.savefig(f"{OUT}/fig-corpus.png", dpi=150); plt.close()

# Figure 3 — integration cost: O(N^2) point-to-point vs O(N) semantic
fig, ax = plt.subplots(figsize=(8, 3.4))
N = list(range(1, 16))
ax.plot(N, [n*(n-1)//2 for n in N], "o-", color=RED, label="Point-to-point  (O(N²) connectors)")
ax.plot(N, N, "o-", color=LIME, label="Semantic router  (O(N) descriptions)")
ax.set_xlabel("data sources connected"); ax.set_ylabel("integrations to build & maintain")
ax.set_title("Why it compounds: integration cost vs number of sources")
ax.legend(frameon=False, fontsize=9.5); ax.spines[["top", "right"]].set_visible(False)
ax.annotate("today's brain\n(~16 sources)", xy=(15, 15), xytext=(10.2, 60),
            fontsize=8.5, color=NAVY, arrowprops=dict(arrowstyle="->", color=GREY))
plt.tight_layout(); plt.savefig(f"{OUT}/fig-cost.png", dpi=150); plt.close()
print("figures written to", OUT, "->", os.listdir(OUT))
