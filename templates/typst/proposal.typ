// Proposal: why/what/how/when/how much, then acceptance.
#import "house.typ": *

#let proposal(data: none, brand: none) = {
  let k = tokens(brand)
  show: doc.with(brand: brand, title: data.title, kind: "Proposal", author: data.at("author", default: ""),
    date: data.at("date", default: none), rev: data.at("rev", default: none))
  set heading(numbering: "1")

  title-block(brand, kind: "Proposal", title: data.title, subtitle: data.at("subtitle", default: none), facts: (
    ("Prepared for", data.client.name),
    ("Date", data.at("date", default: "")),
    ("Proposal no.", data.at("number", default: "—")),
    ("Contact", data.at("author", default: brand.name)),
  ))
  if data.at("summary", default: "") != "" { side("Summary", brand, md(data.summary)); v(0.1in) }

  for s in data.at("sections", default: ()) {
    heading(level: 1, s.title)
    md(s.body)
  }

  let tl = data.at("timeline", default: ())
  if tl.len() > 0 {
    heading(level: 1, "Schedule")
    wide(block(breakable: tl.len() > 12, data-table(brand, columns: (1fr, 1.4in, 1.4in), align: (left, left, left),
      header: ("Phase", "Start", "Duration"),
      rows: tl.map(t => (t.phase, t.start, t.duration)).flatten())))
  }

  let pr = data.at("pricing", default: none)
  if pr != none {
    heading(level: 1, "Investment")
    wide(block(breakable: pr.items.len() > 12, {
      data-table(brand, columns: (1fr, 1.3in), align: (left, right),
        header: ("Item", "Amount (" + pr.symbol + ")"),
        rows: pr.items.map(i => (i.description, money(i.amount))).flatten(),
        footer: (text(weight: "semibold", "Total"), text(weight: "semibold", money(pr.total))))
    }))
    if pr.at("note", default: "") != "" { v(0.05in); text(size: 10pt, md(pr.note)) }
  }

  if data.at("terms", default: ()).len() > 0 {
    v(0.1in)
    side("Terms", brand, { set text(size: 10pt); list(..data.terms.map(md)) })
  }
  v(0.15in)
  block(breakable: false, {
    side("Acceptance", brand, text(size: 10pt)[Sign below to accept this proposal.])
    sign-lines(brand, (data.client.name, brand.name))
  })
}
