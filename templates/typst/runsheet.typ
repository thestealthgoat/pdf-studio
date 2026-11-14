// Run sheet (run of show): landscape, one row per segment, running clock computed in Python.
#import "house.typ": *

#let runsheet(data: none, brand: none) = {
  let k = tokens(brand)
  show: plain-doc.with(brand: brand, title: data.title, kind: "Run sheet", date: data.at("date", default: none),
    rev: data.at("rev", default: none), flipped: true, margin: (x: 0.6in, top: 0.75in, bottom: 0.7in), body-size: 10pt)
  sheet-head(brand, kind: "Run sheet", title: data.title, facts: data.at("facts", default: ()))
  let cols = data.columns
  set text(font: k.sans, size: 9.5pt, number-width: "tabular")
  table(
    columns: cols.map(c => if c.width == "fr" { 1fr } else { eval(c.width) }),
    align: cols.map(c => if c.at("align", default: "left") == "right" { right } else { left }),
    stroke: (x, y) => if y == 0 { (bottom: 0.75pt + k.ink) } else { (bottom: 0.4pt + k.rule) },
    inset: (x: 5pt, y: 5pt),
    fill: (x, y) => if y > 0 and data.rows.at(y - 1).at("kind", default: "") == "break" { k.zebra } else { none },
    table.header(repeat: true, ..cols.map(c => text(size: 8.5pt, weight: "semibold", tracking: 0.04em, fill: k.muted, upper(c.title)))),
    ..data.rows.map(r => cols.map(c => {
      let v = r.at(c.key, default: "")
      if c.key == "segment" { text(weight: "semibold", v) } else { v }
    })).flatten(),
  )
  if data.at("notes", default: "") != "" { v(0.1in); text(size: 9.5pt, md(data.notes)) }
}
