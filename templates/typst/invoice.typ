// Invoice. Uses the same money preparation as estimates; adds payments and balance due.
#import "house.typ": *

#let invoice(data: none, brand: none) = {
  let k = tokens(brand)
  show: doc.with(brand: brand, title: "Invoice " + data.number, kind: "Invoice", keywords: ("invoice", data.number),
    date: data.date)

  title-block(brand, kind: "Invoice", title: "Invoice " + data.number, subtitle: data.at("title", default: none), facts: (
    ("Invoice date", data.date),
    ("Due date", data.valid_until),
    ("PO / reference", data.at("po", default: "—")),
    ("Balance due", data.totals.total),
  ))

  let addr(p) = {
    strong(p.name)
    if p.at("attn", default: "") != "" { linebreak(); [Attn: #p.attn] }
    for l in p.at("address", default: ()) { linebreak(); l }
    if p.at("email", default: "") != "" { linebreak(); p.email }
  }
  wide(grid(
    columns: (gutter - 0.2in, 0.2in, 1fr, 0.3in, 1fr),
    label("Bill to", brand), [], text(font: k.sans, size: 10pt, addr(data.client)),
    [], if data.at("project", default: none) != none {
      label("Project", brand); v(-2pt)
      text(font: k.sans, size: 10pt, { strong(data.project.name); if data.project.at("ref", default: "") != "" { linebreak(); data.project.ref } })
    },
  ))
  v(0.1in)

  for sec in data.sections {
    wide(data-table(brand,
      columns: (0.45in, 1fr, 0.5in, 0.42in, 1.08in, 1.08in),
      align: (left, left, right, left, right, right),
      header: ("Item", "Description", "Qty", "Unit", "Rate (" + data.symbol + ")", "Amount (" + data.symbol + ")"),
      rows: sec.items.map(it => (text(fill: k.muted, it.code), it.description, it.qty, it.unit, money(it.unit_price), money(it.amount))).flatten(),
    ))
  }
  v(0.1in)
  wide(align(right, block(width: 3.9in, breakable: false, {
    set text(font: k.sans, size: 10pt, number-width: "tabular")
    let rows = (([Subtotal], data.totals.subtotal),)
    for adj in data.totals.at("adjustments", default: ()) { rows.push((adj.label, adj.amount)) }
    grid(columns: (1fr, 1.15in), align: (left, right), inset: (x: 5pt, y: 6pt),
      ..rows.enumerate().map(((i, r)) => (if i > 0 { grid.hline(stroke: 0.4pt + k.rule) }, r.at(0), r.at(1))).flatten().filter(x => x != none),
      grid.hline(stroke: 0.75pt + k.ink),
      text(weight: "semibold")[Balance due (#data.currency)], text(weight: "semibold", fill: k.accent, data.totals.total),
      grid.hline(stroke: 0.75pt + k.ink))
  })))

  if data.at("remit", default: ()).len() > 0 {
    v(0.15in)
    side("Remit to", brand, text(font: k.sans, size: 10pt, data.remit.join(linebreak())))
  }
  if data.at("terms", default: ()).len() > 0 {
    v(0.05in)
    side("Terms", brand, { set text(size: 10pt); list(..data.terms.map(md)) })
  }
  if data.at("notes", default: "") != "" { v(0.05in); side("Notes", brand, text(size: 10pt, md(data.notes))) }
}
