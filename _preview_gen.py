"""Dev-only: render weather.py's actual draw calls to a faithful HTML preview.
Not part of the app — used to review the ANSI look without a live terminal."""
import html
import weather

C = weather.curses
_reg = {0: (250, 233)}
C.has_colors = lambda: True
C.COLORS = 256
C.COLOR_PAIRS = 256
C.A_BOLD = 0x10000
C.A_DIM = 0x20000
C.A_NORMAL = 0
C.A_REVERSE = 0x40000
C.init_pair = lambda n, fg, bg: _reg.__setitem__(n, (fg, bg))
C.color_pair = lambda n: n
weather.HAVE_256 = True
weather._PAIR_CACHE.clear()
weather._PAIR_NEXT[0] = 16
weather.THEME = {}
weather.init_theme()
SCREEN = weather.THEME["screen"]


class FakeWin:
    def __init__(self, h, w):
        self.h, self.w = h, w
        self.cells = [[(" ", SCREEN)] * w for _ in range(h)]

    def getmaxyx(self):
        return self.h, self.w

    def erase(self):
        self.cells = [[(" ", SCREEN)] * self.w for _ in range(self.h)]

    def refresh(self):
        pass

    def addnstr(self, r, c, text, n, attr=0):
        pair = attr & 0xFFFF
        for i, ch in enumerate(text[:n]):
            if 0 <= r < self.h and 0 <= c + i < self.w:
                self.cells[r][c + i] = (ch, pair)


def hexc(i):
    if i < 16:
        base = [(0, 0, 0), (205, 0, 0), (0, 205, 0), (205, 205, 0), (0, 0, 238),
                (205, 0, 205), (0, 205, 205), (229, 229, 229), (127, 127, 127),
                (255, 0, 0), (0, 255, 0), (255, 255, 0), (92, 92, 255),
                (255, 0, 255), (0, 255, 255), (255, 255, 255)]
        r, g, b = base[i]
    elif i < 232:
        j = i - 16
        f = lambda v: 0 if v == 0 else 55 + 40 * v
        r, g, b = f((j // 36) % 6), f((j // 6) % 6), f(j % 6)
    else:
        r = g = b = 8 + 10 * (i - 232)
    return "#%02x%02x%02x" % (r, g, b)


def to_html(win, title):
    rows = []
    for row in win.cells:
        spans, run, cf, cb = [], "", None, None
        for ch, pair in row:
            fg, bg = _reg.get(pair, (250, 233))
            fh, bh = hexc(fg), hexc(bg)
            if (fh, bh) != (cf, cb):
                if run:
                    spans.append('<span style="color:%s;background:%s">%s</span>'
                                 % (cf, cb, html.escape(run)))
                run, cf, cb = ch, fh, bh
            else:
                run += ch
        if run:
            spans.append('<span style="color:%s;background:%s">%s</span>'
                         % (cf, cb, html.escape(run)))
        rows.append("".join(spans))
    body = "\n".join(rows)
    return ('<div class="term"><div class="cap">%s</div>'
            '<div class="scroll"><pre>%s</pre></div></div>'
            % (html.escape(title), body))


DATA = {"current_condition": [{"temp_F": "73", "FeelsLikeF": "73", "humidity": "59",
        "windspeedMiles": "4", "winddir16Point": "W", "pressureInches": "30",
        "visibilityMiles": "6", "cloudcover": "45", "uvIndex": "6", "precipInches": "0.0",
        "weatherDesc": [{"value": "Clear"}]}],
        "weather": [{"astronomy": [{"sunrise": "05:57 AM", "sunset": "08:35 PM"}],
        "hourly": [{"time": "1500", "tempF": "86"}, {"time": "1800", "tempF": "83"},
                   {"time": "2100", "tempF": "64"}]},
        {"astronomy": [{"sunrise": "05:58 AM", "sunset": "08:34 PM"}], "hourly": []}]}
AQI = {"aqi": "59", "category": "Moderate", "parameter": "PM2.5"}

import datetime
NOW = datetime.datetime(2026, 8, 8, 7, 25, 0)
sections = []
win = FakeWin(38, 92)
weather.render(win, DATA, NOW, 3, 68, False, None, AQI, None, "Wildfire smoke")
sections.append(to_html(win, "GIG HARBOR TODAY  ·  full BBS-door screen (AQI 59, Moderate)"))

for v, cat, rs in [("18", "Good", None), ("165", "Unhealthy", "Wildfire smoke"),
                    ("305", "Hazardous", "Wildfire smoke")]:
    w2 = FakeWin(38, 92)
    d = dict(DATA)
    weather.render(w2, DATA, NOW, 3, 68, False, None,
                   {"aqi": v, "category": cat}, None, rs)
    sections.append(to_html(w2, "Air quality: %s %s" % (v, cat)))

gauges = "\n".join(sections[1:])
page = """<title>weather.py — ANSI revamp preview</title>
<style>
 :root{--bg:#0a0b12;--ground:%s;--ink:#c3cbdb;--muted:#7f8aa3;
   --cyan:#35d6ff;--gold:#ffd24a;--edge:#20263a}
 *{box-sizing:border-box}
 body{background:
   radial-gradient(120%% 80%% at 50%% -10%%,#12162400,#05060a 70%%),var(--bg);
   color:var(--ink);margin:0;padding:40px 20px 64px;
   font-family:ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;
   line-height:1.5}
 .wrap{max-width:1000px;margin:0 auto}
 .eyebrow{font:600 12px ui-monospace,monospace;letter-spacing:.22em;
   text-transform:uppercase;color:var(--cyan);margin:0 0 8px}
 h1{font-size:26px;font-weight:650;margin:0 0 10px;letter-spacing:-.01em;
   text-wrap:balance}
 .lede{color:var(--muted);max-width:64ch;margin:0 0 28px}
 .lede b{color:var(--ink);font-weight:600}
 h2{font-size:13px;font-weight:600;letter-spacing:.12em;text-transform:uppercase;
   color:var(--muted);margin:36px 0 14px;border-top:1px solid var(--edge);
   padding-top:16px}
 .row{display:flex;flex-wrap:wrap;gap:20px}
 .term{border:1px solid var(--edge);border-radius:8px;overflow:hidden;
   background:var(--ground);box-shadow:0 12px 40px -18px #000,0 0 0 1px #000 inset}
 .term.full{width:100%%}
 .cap{display:flex;align-items:center;gap:8px;
   font:600 12px ui-monospace,monospace;letter-spacing:.04em;
   padding:9px 12px;background:#0e1120;color:var(--gold);
   border-bottom:1px solid var(--edge)}
 .cap::before{content:"";width:9px;height:9px;border-radius:50%%;
   background:var(--cyan);box-shadow:14px 0 0 #ffd24a55,28px 0 0 #ff6b6b55;
   flex:0 0 auto}
 .scroll{overflow-x:auto}
 pre{margin:0;padding:14px 16px;white-space:pre;letter-spacing:0;
   font-family:"DejaVu Sans Mono",ui-monospace,"Menlo","Consolas",monospace;
   font-size:13px;line-height:1.04}
 .legend{margin:28px 0 0;padding:18px 20px;border:1px solid var(--edge);
   border-radius:8px;background:#0c0f1a}
 .legend h3{margin:0 0 10px;font-size:13px;letter-spacing:.1em;
   text-transform:uppercase;color:var(--cyan)}
 .legend ul{margin:0;padding-left:18px;color:var(--muted)}
 .legend li{margin:4px 0}.legend b{color:var(--ink);font-weight:600}
 .note{color:var(--muted);font-size:13px;margin:22px 0 0}
</style>
<div class="wrap">
 <p class="eyebrow">weather.py &middot; terminal dashboard</p>
 <h1>ANSI / BBS-door interface revamp</h1>
 <p class="lede">A faithful render of the actual <b>curses</b> output &mdash; every
  cell shows its true foreground and background color, so half-block glyphs
  (<b>&#9600; &#9617; &#9608;</b>) appear exactly as they do in the terminal. The fuzzy
  dial is gone: air quality is now a <b>linear gradient scale</b> with a pointer,
  and the two panels are matched in height for a cleaner, unified layout.</p>

 <h2>The screen</h2>
 %s

 <h2>Air quality across the scale</h2>
 %s

 <div class="legend"><h3>What changed</h3><ul>
  <li><b>One unified frame</b>: PNW dawn hero up top, a two-column body
   (conditions | air quality) split by a shared divider &mdash; a real BBS door.</li>
  <li><b>&ldquo;GIG HARBOR&rdquo;</b> block-letter title with a sunrise gradient over a
   scene: snow-capped peaks, evergreens, the Sound, a sun, stars, a sailboat.</li>
  <li><b>Linear AQI scale</b> with a pointer + beveled number plate.</li>
  <li><b>Full-gradient palette</b> throughout &mdash; sky, water, bevels, meters, scale.</li>
 </ul></div>
 <p class="note">Rendered headlessly from weather.py&#39;s draw calls. Run
  <code>python3 weather.py</code> for the live version.</p>
</div>
""" % (hexc(17),
       sections[0].replace('class="term"', 'class="term full"', 1),
       "\n".join(s.replace('class="term"', 'class="term full"', 1) for s in sections[1:]))

with open("preview.html", "w") as f:
    f.write(page)
print("wrote preview.html  (%d sections)" % len(sections))

# Also emit a plain-text sanity view of the full dashboard.
print("\n".join("".join(ch for ch, _ in row).rstrip() for row in win.cells[:0]))
