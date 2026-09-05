/* ReconProof dashboard.
 *
 * Plain ES module, no framework, no build step. The whole file is fetch,
 * format, and build DOM nodes; there is no state machine beyond `state`
 * below, because there are four screens and one payload.
 *
 * Nothing here computes anything about money. Every figure on screen came
 * from the server as integer paise and is only formatted here — if the
 * browser could do arithmetic on amounts, the dashboard would be a second,
 * unverified implementation of the ledger identity.
 */

const state = {
  report: null,
  view: "ledger",
  filter: "all",
  sort: "date",
  openRow: null,
  /** proof_id -> verdict, from before the last tamper, so we can say what flipped. */
  verdictsBefore: new Map(),
};

/* -- formatting ---------------------------------------------------------- */

/** Indian digit grouping: 2,2,3 from the right. ₹4,14,382.00, never ₹414,382.00. */
function groupIndian(digits) {
  if (digits.length <= 3) return digits;
  const last3 = digits.slice(-3);
  const rest = digits.slice(0, -3);
  return rest.replace(/\B(?=(\d{2})+(?!\d))/g, ",") + "," + last3;
}

/** Integer paise -> "₹4,14,382.00", negatives in accounting parentheses. */
function rupees(paise) {
  if (paise === null || paise === undefined) return "—";
  const negative = paise < 0;
  const abs = Math.abs(paise);
  const whole = groupIndian(String(Math.floor(abs / 100)));
  const fraction = String(abs % 100).padStart(2, "0");
  const text = `₹${whole}.${fraction}`;
  return negative ? `(${text})` : text;
}

/** A zero residual is an absence, not a quantity. Render it as a dash. */
function residualText(paise) {
  if (paise === null || paise === undefined) return "—";
  return paise === 0 ? "—" : rupees(paise);
}

function pct(fraction) {
  return `${(fraction * 100).toFixed(1)}%`;
}

function shortDate(iso) {
  const [y, m, d] = iso.split("-");
  const months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  return `${d} ${months[Number(m) - 1]} ${y}`;
}

function amountClass(paise) {
  if (paise === 0 || paise === null || paise === undefined) return "num zero";
  return paise < 0 ? "num neg" : "num";
}

/* -- tiny DOM helpers ---------------------------------------------------- */

function el(tag, props = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key === "html") node.innerHTML = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (value !== null && value !== undefined) node.setAttribute(key, value);
  }
  for (const child of [].concat(children)) {
    if (child) node.appendChild(child);
  }
  return node;
}

function cell(tag, className, text) {
  return el(tag, { class: className, text });
}

function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}

/* -- server -------------------------------------------------------------- */

async function api(path, options) {
  const response = await fetch(path, options);
  if (!response.ok) {
    const detail = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(detail.detail || "request failed");
  }
  return response.json();
}

async function load(path = "/api/report", options) {
  state.report = await api(path, options);
  renderAll();
  return state.report;
}

/* -- masthead and summary ------------------------------------------------ */

function renderRunLine() {
  const { run, totals, params } = state.report;
  const bits = [
    `seed ${run.seed}`,
    run.difficulty,
    `${totals.records} records`,
    `${totals.wall_clock_seconds}s`,
    `abstain below ${params.abstain_below}`,
  ];
  if (state.report.tampered.length) {
    bits.push(`${state.report.tampered.length} record(s) tampered`);
  }
  document.getElementById("run-line").textContent = bits.join(" · ");
}

function summaryItem(label, value, tone) {
  return el("div", { class: "summary-item" }, [
    el("span", { class: "summary-label", text: label }),
    el("span", { class: `summary-value${tone ? " " + tone : ""}`, text: value }),
  ]);
}

/* Loud, because a page quoting numbers from data it corrupted itself is the
 * one failure this project cannot afford to ship. */
function renderTamperBanner() {
  const banner = document.getElementById("tamper-banner");
  const changes = state.report.tampered;
  banner.hidden = changes.length === 0;
  if (!changes.length) return;
  document.getElementById("tamper-banner-detail").textContent = changes
    .map((c) => `${c.record_id}.${c.field} ${rupees(c.before)} → ${rupees(c.after)}`)
    .join(", ");
}

function renderSummary() {
  const m = state.report.metrics;
  const llm = state.report.llm;
  const node = document.getElementById("summary");
  clear(node);

  node.appendChild(summaryItem("Matched", `${m.matched_credits} / ${m.live_credits}`));
  node.appendChild(summaryItem("Value covered", pct(m.value_coverage)));
  node.appendChild(summaryItem(
    "False matches",
    String(m.false_matches),
    m.false_matches === 0 ? "is-good" : "is-flag",
  ));
  node.appendChild(summaryItem("Exceptions", String(m.exceptions), "is-flag"));
  node.appendChild(summaryItem("At stake", rupees(m.exception_value_at_stake_paise)));
  node.appendChild(summaryItem(
    "Model",
    llm.enabled ? `${llm.proposed} proposed / ${llm.accepted_by_verifier} kept` : "off",
  ));
}

/* -- ledger -------------------------------------------------------------- */

function sortedLedger() {
  const rows = state.report.ledger.filter((row) => {
    if (state.filter === "all") return true;
    return row.status === state.filter;
  });
  const by = {
    date: (a, b) => a.value_date.localeCompare(b.value_date) || a.bank_txn_id.localeCompare(b.bank_txn_id),
    amount: (a, b) => b.credit_amount - a.credit_amount,
    residual: (a, b) => Math.abs(b.residual ?? 0) - Math.abs(a.residual ?? 0),
  };
  return rows.slice().sort(by[state.sort]);
}

function proofById(proofId) {
  return state.report.proofs.find((p) => p.proof_id === proofId) || null;
}

function idButton(recordId) {
  return el("button", {
    class: "id-link",
    type: "button",
    text: recordId,
    title: `Show the raw ${recordId} record`,
    onclick: (event) => {
      event.stopPropagation();
      showRecord(recordId);
    },
  });
}

function sourceIds(ids) {
  if (!ids || !ids.length) return null;
  const wrap = el("span", { class: "term-sources" });
  const shown = ids.slice(0, 6);
  shown.forEach((id) => wrap.appendChild(idButton(id)));
  if (ids.length > shown.length) {
    wrap.appendChild(el("span", { text: ` +${ids.length - shown.length} more` }));
  }
  return wrap;
}

/** The proof panel: the ledger identity, rendered as a derivation. */
function derivation(proof, row) {
  const wrap = el("div", { class: "derivation" });
  wrap.appendChild(el("h3", {
    text: `${proof.proof_id} · ${proof.rule}` +
          (proof.settlement_id ? ` · ${proof.settlement_id}` : ""),
  }));

  const table = el("table", { class: "terms" });
  const body = el("tbody");

  for (const term of proof.terms) {
    const label = el("td", { class: "label" }, [
      el("span", { text: `${term.label} (${term.source_ids.length})` }),
      sourceIds(term.source_ids),
    ]);
    const signed = term.sign * term.amount;
    body.appendChild(el("tr", {}, [label, cell("td", amountClass(signed), rupees(signed))]));
  }

  body.appendChild(el("tr", { class: "rule-single" }, [
    cell("td", "label", "Computed net"),
    cell("td", "num", rupees(proof.computed_net)),
  ]));
  body.appendChild(el("tr", {}, [
    cell("td", "label", "Observed credit"),
    cell("td", "num", rupees(proof.observed_credit)),
  ]));
  body.appendChild(el("tr", { class: "rule-double" }, [
    cell("td", "label", "Residual"),
    cell("td", amountClass(proof.residual), residualText(proof.residual)),
  ]));

  table.appendChild(body);
  wrap.appendChild(table);

  const passed = proof.verdict === "PASS";
  const stamp = el("div", {
    class: `stamp ${passed ? "pass" : "fail"}`,
    text: passed ? "Verified" : "Failed",
  });
  stamp.dataset.proofId = proof.proof_id;
  wrap.appendChild(stamp);

  if (!passed && proof.verdict_reason) {
    wrap.appendChild(el("p", { class: "derivation-note", text: proof.verdict_reason }));
  }
  if (row && row.what_to_check) {
    wrap.appendChild(el("p", { class: "derivation-note", text: row.what_to_check }));
  }

  const suspects = proof.evidence.filter((e) => e.check === "residual_suspects");
  for (const entry of suspects) {
    wrap.appendChild(el("p", { class: "derivation-meta", text: `Check: ${entry.value}` }));
  }
  const changed = proof.evidence.filter((e) => e.check === "observed_credit");
  for (const entry of changed) {
    wrap.appendChild(el("p", { class: "derivation-meta", text: entry.note }));
  }

  wrap.appendChild(el("p", {
    class: "derivation-meta",
    text: `confidence ${proof.confidence}` +
          (row && row.case_type ? ` · generator label: ${row.case_type}` : ""),
  }));
  return wrap;
}

function toggleRow(txnId) {
  state.openRow = state.openRow === txnId ? null : txnId;
  writeHash();
  renderLedger();
  if (state.openRow) {
    const open = document.querySelector(`[data-open-for="${state.openRow}"]`);
    if (open) open.scrollIntoView({ block: "nearest" });
  }
}

function renderLedger() {
  const body = document.getElementById("ledger-body");
  const empty = document.getElementById("ledger-empty");
  clear(body);

  const rows = sortedLedger();
  if (!rows.length) {
    empty.hidden = false;
    empty.textContent = state.filter === "exception"
      ? "No exceptions at this confidence threshold. Lower it on the Tamper screen to see borderline matches."
      : "Nothing verified at this threshold.";
    return;
  }
  empty.hidden = true;

  rows.forEach((row, index) => {
    // The greenbar band is a class on the rows, not a <tbody> per credit.
    // Nesting a <tbody> inside the <tbody> produced an anonymous inner table
    // whose columns ignored the colgroup, so the header obeyed the fixed
    // layout and the body rows sized themselves by content and overflowed.
    // See WHAT_BROKE.md, 5 Sep.
    const band = index % 2 ? "band alt" : "band";
    const open = state.openRow === row.bank_txn_id;
    const passed = row.status === "matched";

    const neutral = row.status === "neutralised";
    const verdict = el("td", { class: "col-verdict" }, [
      el("span", {
        class: `verdict-word ${neutral ? "neutral" : passed ? "pass" : "fail"}`,
        text: neutral ? "Reversed" : passed ? "Verified" : "Exception",
      }),
      neutral
        ? el("span", { class: "reason-code", text: `cancels ${row.reversed_by}` })
        : row.reason_code
          ? el("span", { class: "reason-code", text: row.reason_code })
          : null,
    ]);

    const tr = el("tr", {
      class: `ledger-row ${band}${neutral ? " is-neutralised" : ""}`,
      tabindex: "0",
      "aria-expanded": open ? "true" : "false",
      onclick: () => toggleRow(row.bank_txn_id),
      onkeydown: (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          toggleRow(row.bank_txn_id);
        }
      },
    }, [
      el("td", { class: "col-id" }, [
        el("span", { class: "disclosure", text: open ? "▾" : "▸" }),
        el("span", { text: row.bank_txn_id }),
      ]),
      cell("td", "col-date", shortDate(row.value_date)),
      el("td", { class: "col-narration", title: row.narration, text: row.narration }),
      cell("td", amountClass(row.credit_amount), rupees(row.credit_amount)),
      cell("td", "col-rule", row.rule || "—"),
      cell("td", amountClass(row.residual), residualText(row.residual)),
      verdict,
    ]);
    body.appendChild(tr);

    if (open) {
      const proof = proofById(row.proof_id);
      const panel = el("tr", { class: `derivation-row ${band}`, "data-open-for": row.bank_txn_id }, [
        el("td", { colspan: "7" }, [
          neutral
            ? el("p", {
                class: "empty",
                text: `${row.bank_txn_id} and ${row.reversed_by} are equal and ` +
                      "opposite on the same UTR and the same day: a credit the " +
                      "bank posted in error and pulled back. Neither leg is " +
                      "matchable, and both are excluded from the match rate " +
                      "rather than counted as failures.",
              })
            : proof
              ? derivation(proof, row)
              : el("p", {
                  class: "empty",
                  text: `No candidate proof was generated for ${row.bank_txn_id}. ` +
                        (row.what_to_check || ""),
                }),
        ]),
      ]);
      body.appendChild(panel);
    }
  });
}

/* -- exceptions ---------------------------------------------------------- */

function renderExceptions() {
  const lede = document.getElementById("exceptions-lede");
  const host = document.getElementById("exceptions-groups");
  clear(host);

  const exceptions = state.report.exceptions;
  const m = state.report.metrics;
  lede.textContent =
    `${exceptions.length} things the system refused to match, ` +
    `${rupees(m.exception_value_at_stake_paise)} at stake. ` +
    `Every one carries a reason code, a residual, and what a human should check. ` +
    `Sorted by rupees at stake, which is the order to work them in.`;

  const groups = new Map();
  for (const exception of exceptions) {
    if (!groups.has(exception.reason_code)) groups.set(exception.reason_code, []);
    groups.get(exception.reason_code).push(exception);
  }

  const ordered = [...groups.entries()].sort((a, b) => {
    const stake = (list) => list.reduce((sum, e) => sum + e.at_stake, 0);
    return stake(b[1]) - stake(a[1]);
  });

  for (const [reason, list] of ordered) {
    const stake = list.reduce((sum, e) => sum + e.at_stake, 0);
    const group = el("section", { class: "exception-group" });
    group.appendChild(el("h2", {}, [
      el("span", { text: reason }),
      el("span", {
        class: "count",
        text: `${list.length} · ${rupees(stake)} at stake`,
      }),
    ]));

    const table = el("table", { class: "exceptions" });
    const cols = el("colgroup");
    ["x-subject", "x-amount", "x-amount", "x-records", "x-check"].forEach((name) => {
      cols.appendChild(el("col", { class: name }));
    });
    table.appendChild(cols);
    table.appendChild(el("thead", {}, [
      el("tr", {}, [
        cell("th", "", "Subject"),
        cell("th", "col-num", "Residual"),
        cell("th", "col-num", "At stake"),
        cell("th", "", "Records"),
        cell("th", "", "What to check"),
      ]),
    ]));

    const body = el("tbody");
    for (const exception of list.slice().sort((a, b) => b.at_stake - a.at_stake)) {
      const records = el("td", { class: "records" });
      exception.records_involved.slice(0, 4).forEach((id) => records.appendChild(idButton(id)));
      body.appendChild(el("tr", {}, [
        el("td", { class: "subject" }, [idButton(exception.subject_id)]),
        cell("td", amountClass(exception.residual), residualText(exception.residual)),
        cell("td", "num", rupees(exception.at_stake)),
        records,
        cell("td", "check", exception.what_to_check),
      ]));
    }
    table.appendChild(body);
    group.appendChild(table);
    host.appendChild(group);
  }
}

/* -- scorecard ----------------------------------------------------------- */

function figureRows(host, rows, headerCells) {
  clear(host);
  if (headerCells) {
    host.appendChild(el("thead", {}, [
      el("tr", {}, headerCells.map((h, i) =>
        cell("th", i === 0 ? "" : "col-num", h))),
    ]));
  }
  const body = el("tbody");
  for (const row of rows) {
    const tr = el("tr", { class: row.className || "" });
    row.cells.forEach((value, index) => {
      tr.appendChild(cell("td", index === 0 ? "key" : "num", value));
    });
    body.appendChild(tr);
  }
  host.appendChild(body);
}

function renderScorecard() {
  const m = state.report.metrics;
  const totals = state.report.totals;

  figureRows(document.getElementById("scorecard-headline"), [
    { cells: ["Auto-match rate (bank credits)", `${pct(m.auto_match_rate_records)}  (${m.matched_credits}/${m.live_credits})`], className: "headline" },
    { cells: ["Value coverage", pct(m.value_coverage)] },
    { cells: ["Value matched", rupees(m.value_matched_paise)] },
    { cells: ["False-match rate", `${pct(m.false_match_rate)}  (${m.false_matches}/${m.accepted_matches})`], className: "headline" },
    { cells: ["Exceptions filed", String(m.exceptions)] },
    { cells: ["At stake in exceptions", rupees(m.exception_value_at_stake_paise)] },
    { cells: ["Exception coverage", pct(m.exception_coverage)] },
    { cells: ["Diagnosis accuracy", `${pct(m.diagnosis_accuracy)}  (${m.correct_diagnoses}/${m.unmatched_units})`] },
    { cells: ["Throughput", `${totals.records} in ${totals.wall_clock_seconds}s`] },
    { cells: ["Subset cap hits", String(m.subset_cap_hits)] },
  ]);

  figureRows(
    document.getElementById("scorecard-calibration"),
    state.report.calibration.map((bucket) => ({
      cells: [
        bucket.bucket,
        String(bucket.proofs),
        `${bucket.correct}/${bucket.proofs}`,
        bucket.accepted ? "accepted" : "abstained",
      ],
    })),
    ["Confidence", "Proofs", "Correct", "Outcome"],
  );

  const llm = state.report.llm;
  figureRows(document.getElementById("scorecard-llm"), llm.enabled ? [
    { cells: ["Hypotheses proposed", String(llm.proposed)] },
    { cells: ["Accepted by the verifier", String(llm.accepted_by_verifier)] },
    { cells: ["Rejected by the verifier", String(llm.rejected_by_verifier)] },
  ] : [
    { cells: ["Adjudication layer", "off (--no-llm)"] },
    { cells: ["Everything above", "deterministic"] },
  ]);

  // Sorted worst-first: the point of this table is the rows near the top.
  const cases = Object.entries(state.report.by_case_type)
    .map(([name, row]) => ({ name, ...row }))
    .sort((a, b) => (a.matched / a.units) - (b.matched / b.units) || a.name.localeCompare(b.name));

  figureRows(
    document.getElementById("scorecard-cases"),
    cases.map((row) => ({
      className: row.matched === 0 && row.expected_outcome === "match" ? "is-weak" : "",
      cells: [row.name, String(row.units), String(row.matched), String(row.exceptions), pct(row.handled_rate)],
    })),
    ["Case type", "Units", "Matched", "Exceptions", "Handled"],
  );
}

/* -- tamper -------------------------------------------------------------- */

function renderTamperControls() {
  const select = document.getElementById("tamper-record");
  if (select.options.length) return;

  // Only records that are actually inside an accepted proof: tampering with
  // something nothing depends on proves nothing, and a demo that proves
  // nothing is worse than no demo.
  const accepted = new Set(state.report.accepted_proof_ids);
  const options = new Set();
  for (const proof of state.report.proofs) {
    if (!accepted.has(proof.proof_id)) continue;
    (proof.members.payment_ids || []).slice(0, 3).forEach((id) => options.add(id));
    (proof.members.bank_txn_ids || []).forEach((id) => options.add(id));
  }
  [...options].sort().forEach((id) => {
    select.appendChild(el("option", { value: id, text: id }));
  });
}

function verdictSnapshot() {
  const map = new Map();
  for (const proof of state.report.proofs) map.set(proof.proof_id, proof);
  return map;
}

/** Proofs whose member set contains a record, whatever their verdict. */
function proofsTouching(recordId) {
  return state.report.proofs.filter((proof) =>
    Object.values(proof.members).some((ids) => ids.includes(recordId)),
  );
}

/* The panel is rendered from `report.tampered`, which is server state, so a
 * reload still shows what has been corrupted. `before` is the client's
 * snapshot from the moment of the last submit and only adds the "was PASS"
 * half of the sentence; without it the panel still tells the truth. */
function renderTamper(before) {
  const log = document.getElementById("tamper-log");
  clear(log);

  const changes = state.report.tampered;
  if (!changes.length) {
    log.appendChild(el("p", {
      class: "empty",
      text: "Nothing corrupted. Pick a record above and shift it by a few " +
            "thousand paise: the verifier re-reads the source data, the " +
            "affected derivation stops balancing, and its stamp flips to " +
            "failed. Restore puts the batch back by re-reading the committed " +
            "CSVs.",
    }));
    return;
  }

  for (const change of changes.slice().reverse()) {
    const entry = el("div", { class: "tamper-entry" });
    entry.appendChild(el("p", {
      class: "what",
      text: `${change.record_id}.${change.field}  ${rupees(change.before)} → ${rupees(change.after)}`,
    }));

    const touched = proofsTouching(change.record_id);
    const failing = touched.filter((proof) => proof.verdict !== "PASS");

    if (!failing.length) {
      entry.appendChild(el("p", {
        class: "aside",
        text: touched.length
          ? "No verdict changed. Every proof holding that record had already failed."
          : "No verdict changed: that record is not a member of any proof, so " +
            "nothing depended on it.",
      }));
    }

    for (const proof of failing) {
      const was = before && before.get(proof.proof_id);
      const line = el("p", { class: "flip" }, [
        el("span", { text: `${proof.proof_id} · ${proof.bank_txn_id}  ` }),
        was ? el("span", { class: "from", text: was.verdict }) : null,
        was ? el("span", { text: " → " }) : null,
        el("span", { class: "to", text: proof.verdict }),
      ]);
      if (proof.verdict_reason) {
        line.appendChild(el("span", { class: "term", text: `   ${proof.verdict_reason}` }));
      }
      line.appendChild(el("span", {
        class: "term",
        text: `   residual ${residualText(proof.residual)}`,
      }));
      entry.appendChild(line);
    }

    entry.appendChild(el("p", {
      class: "aside",
      text: "The proofs were not rebuilt. The verifier re-read the source " +
            "records and recomputed the identity, and that is the only thing " +
            "that changed.",
    }));
    log.appendChild(entry);
  }
}

function restamp(proofId) {
  const stamp = document.querySelector(`.stamp[data-proof-id="${proofId}"]`);
  if (!stamp) return;
  stamp.classList.add("restamp");
  stamp.addEventListener("animationend", () => stamp.classList.remove("restamp"), { once: true });
}

async function doTamper(event) {
  event.preventDefault();
  const recordId = document.getElementById("tamper-record").value;
  const delta = Number(document.getElementById("tamper-delta").value);
  const before = verdictSnapshot();
  try {
    state.report = await api("/api/tamper", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ record_id: recordId, delta_paise: delta }),
    });
    renderAll();
    renderTamper(before);
    const flipped = state.report.proofs.find((proof) => {
      const was = before.get(proof.proof_id);
      return was && was.verdict !== proof.verdict;
    });
    if (flipped) restamp(flipped.proof_id);
  } catch (error) {
    clear(document.getElementById("tamper-log"));
    document.getElementById("tamper-log").appendChild(
      el("p", { class: "empty", text: error.message }),
    );
  }
}

async function doRestore() {
  await load("/api/restore", { method: "POST" });
}

/* -- record sheet -------------------------------------------------------- */

async function showRecord(recordId) {
  const sheet = document.getElementById("record-sheet");
  const title = document.getElementById("record-sheet-title");
  const fields = document.getElementById("record-fields");
  try {
    const record = await api(`/api/record/${encodeURIComponent(recordId)}`);
    title.textContent = `${record.kind} · ${recordId}`;
    const rows = Object.entries(record.fields).map(([key, value]) => ({
      cells: [key, typeof value === "number" && /amount|fee|tax|net|credit|total/.test(key)
        ? rupees(value)
        : String(value)],
    }));
    if (record.tampered) {
      rows.push({
        className: "is-weak",
        cells: ["tampered", `${rupees(record.tampered.before)} → ${rupees(record.tampered.after)}`],
      });
    }
    figureRows(fields, rows);
    sheet.hidden = false;
    document.getElementById("record-close").focus();
  } catch (error) {
    title.textContent = recordId;
    figureRows(fields, [{ cells: ["error", error.message] }]);
    sheet.hidden = false;
  }
}

/* -- view switching ------------------------------------------------------ */
/* The location hash is the view: #/exceptions, or #/ledger/bnk_0011 to open
 * one derivation. Cheap to implement and it means a specific proof can be
 * linked to - useful in a demo, and the thing you always want when someone
 * asks "which row was that". */

const VIEWS = ["ledger", "exceptions", "scorecard", "tamper"];

function setView(view, { push = true } = {}) {
  state.view = VIEWS.includes(view) ? view : "ledger";
  for (const tab of document.querySelectorAll(".tab")) {
    if (tab.dataset.view === state.view) tab.setAttribute("aria-current", "page");
    else tab.removeAttribute("aria-current");
  }
  for (const section of document.querySelectorAll(".view")) {
    section.hidden = section.id !== `view-${state.view}`;
  }
  if (push) writeHash();
}

function writeHash() {
  const suffix = state.view === "ledger" && state.openRow ? `/${state.openRow}` : "";
  const next = `#/${state.view}${suffix}`;
  if (location.hash !== next) history.replaceState(null, "", next);
}

function applyHash() {
  const [, view, row] = (location.hash || "#/ledger").split("/");
  state.openRow = row || null;
  setView(view || "ledger", { push: false });
  if (state.report) renderLedger();
}

function renderAll() {
  renderRunLine();
  renderSummary();
  renderLedger();
  renderExceptions();
  renderScorecard();
  renderTamperControls();
  renderTamper(null);
  renderTamperBanner();
}

/* -- wiring -------------------------------------------------------------- */

document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => setView(tab.dataset.view));
});

document.querySelectorAll(".chip").forEach((chip) => {
  chip.addEventListener("click", () => {
    state.filter = chip.dataset.filter;
    state.openRow = null;
    document.querySelectorAll(".chip").forEach((c) => c.classList.toggle("is-on", c === chip));
    renderLedger();
  });
});

document.getElementById("ledger-sort").addEventListener("change", (event) => {
  state.sort = event.target.value;
  renderLedger();
});

document.getElementById("tamper-form").addEventListener("submit", doTamper);
document.getElementById("tamper-restore").addEventListener("click", doRestore);
document.getElementById("tamper-banner-restore").addEventListener("click", doRestore);
document.getElementById("record-close").addEventListener("click", () => {
  document.getElementById("record-sheet").hidden = true;
});
document.getElementById("record-sheet").addEventListener("click", (event) => {
  if (event.target.id === "record-sheet") event.target.hidden = true;
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") document.getElementById("record-sheet").hidden = true;
});

window.addEventListener("hashchange", applyHash);

applyHash();
load().catch((error) => {
  document.getElementById("run-line").textContent = `could not load: ${error.message}`;
});
