// Estimate / quote. Data is prepared by pdfcore.ops.typeset (amounts pre-computed as strings).
#import "house.typ": *

#let estimate(data: none, brand: none) = {
  let k = tokens(brand)
  show: doc.with(
    brand: brand,
    title: data.title,
    kind: data.kind,
    author: data.prepared_by.name,
    keywords: ("estimate", data.number),
    date: data.date,
    rev: data.at("rev", default: none),
  )

  title-block(
    brand,
    kind: data.kind,
    title: data.title,
    subtitle: data.at("subtitle", default: none),
    facts: (
      (data.kind + " no.", data.number),
      ("Date", data.date),
      ("Valid until", data.valid_until),
      ("Prepared by", data.prepared_by.name),
    ),
  )

  // Parties
  let addr(p) = {
    strong(p.name)
    if p.at("attn", default: "") != "" { linebreak(); [Attn: #p.attn] }
    for l in p.at("address", default: ()) { linebreak(); l }
    if p.at("email", default: "") != "" { linebreak(); p.email }
    if p.at("phone", default: "") != "" { linebreak(); p.phone }
  }
  wide(grid(
    columns: (gutter - 0.2in, 0.2in, 1fr, 0.3in, 1fr),
    label("Prepared for", brand), [], text(font: k.sans, size: 10pt, addr(data.client)),
    [], {
      label("Project", brand)
      v(-2pt)
      text(font: k.sans, size: 10pt, {
        strong(data.project.name)
        for l in data.project.at("location", default: ()) { linebreak(); l }
        if data.project.at("ref", default: "") != "" { linebreak(); [Ref: #data.project.ref] }
      })
    },
  ))
  v(0.15in)

  if data.at("summary", default: "") != "" {
    side("Scope", brand, md(data.summary))
    v(0.1in)
  }

  // Line items, one table per section
  for sec in data.sections {
    wide(block(breakable: sec.items.len() > 14, {
      block(above: 1.4em, below: 0.6em, text(font: k.sans, size: 10pt, weight: "semibold", sec.name))
      data-table(
        brand,
        columns: (0.45in, 1fr, 0.42in, 0.42in, 1.08in, 1.08in),
        align: (left, left, right, left, right, right),
        header: ("Item", "Description", "Qty", "Unit", "Unit price (" + data.symbol + ")", "Amount (" + data.symbol + ")"),
        rows: sec.items.map(it => (
          text(fill: k.muted, it.code),
          {
            it.description
            if it.at("note", default: "") != "" { linebreak(); text(size: 8.5pt, fill: k.muted, it.note) }
          },
          it.qty, it.unit, money(it.unit_price), money(it.amount),
        )).flatten(),
        footer: (
          table.cell(colspan: 5, align: right, text(weight: "semibold", [#sec.name subtotal])),
          text(weight: "semibold", money(sec.subtotal)),
        ),
      )
    }))
  }

  // Totals
  v(0.1in)
  wide(align(right, block(width: 3.9in, breakable: false, {
    set text(font: k.sans, size: 10pt, number-width: "tabular")
    let rows = (([Subtotal], data.totals.subtotal),)
    for adj in data.totals.at("adjustments", default: ()) { rows.push((adj.label, adj.amount)) }
    grid(
      columns: (1fr, 1.15in),
      align: (left, right),
      inset: (x: 5pt, y: 6pt),
      ..rows.enumerate().map(((i, r)) => (
        if i > 0 { grid.hline(stroke: 0.4pt + k.rule) },
        r.at(0), r.at(1),
      )).flatten().filter(x => x != none),
      grid.hline(stroke: 0.75pt + k.ink),
      text(weight: "semibold")[Total (#data.currency)], text(weight: "semibold", data.totals.total),
      grid.hline(stroke: 0.75pt + k.ink),
    )
  })))

  if data.at("alternates", default: ()).len() > 0 {
    wide({
      block(above: 1.6em, below: 0.6em, text(font: k.sans, size: 10pt, weight: "semibold", "Alternates"))
      data-table(
        brand,
        columns: (0.5in, 1fr, 1.6in),
        align: (left, left, right),
        header: ("No.", "Description", "Add / (deduct) (" + data.symbol + ")"),
        rows: data.alternates.map(a => (text(fill: k.muted, a.code), a.description, money(a.amount))).flatten(),
      )
    })
  }

  let listing(items) = {
    set text(size: if k.body-size == 11pt { 10pt } else { 9.5pt })
    list(..items.map(i => md(i)))
  }
  for (lbl, key) in (("Inclusions", "inclusions"), ("Exclusions", "exclusions"), ("Clarifications", "clarifications"), ("Terms", "terms")) {
    let items = data.at(key, default: ())
    if items.len() > 0 {
      v(0.02in)
      side(lbl, brand, listing(items))
    }
  }

  if data.at("acceptance", default: true) {
    v(0.08in)
    block(breakable: false, {
      side("Acceptance", brand, text(size: 10pt)[
        To accept this #lower(data.kind), sign below and return it to #brand.name. Work will be scheduled on receipt of
        the signed #lower(data.kind)#if data.at("deposit", default: "") != "" [ and the deposit of #data.deposit].
      ])
      sign-lines(brand, (data.client.name, brand.name))
    })
  }
}
