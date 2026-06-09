"use strict";

const STATUS_CLASS = {
  published: "ok", generated: "ok",
  publishing: "warn", generating: "warn", pending: "gray",
  failed: "bad", blocked: "bad",
};

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
}

function scoreClass(v) {
  if (v == null) return "";
  if (v >= 80) return "ok";
  if (v >= 60) return "warn";
  return "bad";
}

function scoreCell(v) {
  const td = el("td", "num");
  if (v == null) { td.append(el("span", "dash", "—")); return td; }
  td.append(el("span", "score " + scoreClass(v), String(v)));
  return td;
}

function statusPill(status) {
  return el("span", "pill " + (STATUS_CLASS[status] || "gray"), status || "—");
}

function distCell(dists) {
  const td = el("td");
  if (!dists || dists.length === 0) { td.append(el("span", "dash", "—")); return td; }
  const wrap = el("div", "dist");
  for (const d of dists) {
    const label = `${d.platform}${d.account && d.account !== "default" ? "/" + d.account : ""}` +
                  `${d.lang && d.lang !== "zh" ? " " + d.lang : ""}`;
    const cls = STATUS_CLASS[d.publish_status] || "gray";
    wrap.append(el("span", "pill " + cls, label));
  }
  td.append(wrap);
  return td;
}

function gateCell(art) {
  const td = el("td");
  if (art.publish_blocked == null) { td.append(el("span", "dash", "—")); return td; }
  if (art.publish_blocked) {
    td.append(el("span", "pill bad", "BLOCKED"));
    if (art.block_reason) td.append(el("div", "reason", art.block_reason));
  } else {
    td.append(el("span", "pill ok", "通过"));
  }
  return td;
}

function renderStats(s) {
  const box = document.getElementById("stats");
  box.replaceChildren();
  const mk = (label, val, cls) => {
    const c = el("span", "chip" + (cls ? " " + cls : ""));
    c.append(el("b", null, String(val)), document.createTextNode(" " + label));
    return c;
  };
  box.append(
    mk("文章", s.total),
    mk("已生成", s.generated, "ok"),
    mk("已发布", s.published, "ok"),
    mk("被拦", s.blocked, s.blocked ? "bad" : ""),
  );
}

function renderRows(items) {
  const tbody = document.getElementById("rows");
  tbody.replaceChildren();
  if (!items.length) {
    const tr = el("tr");
    tr.append(el("td", "muted")).firstChild.colSpan = 8;
    tr.firstChild.textContent = "还没有文章。先跑 python batch_processor.py --stage generate";
    tbody.append(tr);
    return;
  }
  for (const a of items) {
    const tr = el("tr");
    tr.append(el("td", "job", a.job_id));
    tr.append(el("td", "title", a.title || "—"));
    const stTd = el("td"); stTd.append(statusPill(a.status)); tr.append(stTd);
    tr.append(scoreCell(a.markdown_health_score));
    tr.append(scoreCell(a.tonal_score));
    tr.append(gateCell(a));
    tr.append(distCell(a.distributions));
    const act = el("td");
    if (a.title) {
      const link = el("a", "preview", "预览");
      link.href = "/preview/" + encodeURIComponent(a.job_id);
      link.target = "_blank";
      act.append(link);
    } else {
      act.append(el("span", "dash", "—"));
    }
    tr.append(act);
    tbody.append(tr);
  }
}

async function load() {
  try {
    const res = await fetch("/api/articles");
    const data = await res.json();
    renderStats(data.stats);
    renderRows(data.articles);
  } catch (e) {
    document.getElementById("rows").innerHTML =
      '<tr><td colspan="8" class="muted">加载失败：' + e + "</td></tr>";
  }
}

document.getElementById("refresh").addEventListener("click", load);
load();
