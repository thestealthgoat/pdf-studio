// Production sheet: crew positions, call times, gear, cues — portrait, full width.
#import "house.typ": *

#let production(data: none, brand: none) = {
  let k = tokens(brand)
  show: plain-doc.with(brand: brand, title: data.title, kind: "Production sheet", date: data.at("date", default: none),
    rev: data.at("rev", default: none), body-size: 10pt)
  sheet-head(brand, kind: "Production sheet", title: data.title, facts: data.at("facts", default: ()))
  let tbl(headers, widths, rows) = data-table(brand, columns: widths.map(w => if w == "fr" { 1fr } else { eval(w) }),
    align: widths.map(_ => left), header: headers, rows: rows.flatten(), size: 9.5pt)
  for sec in data.sections {
    block(breakable: sec.rows.len() > 18, {
      heading(level: 1, sec.title)
      if sec.at("note", default: "") != "" { text(size: 9.5pt, fill: k.muted, md(sec.note)); v(0.04in) }
      tbl(sec.headers, sec.widths, sec.rows)
    })
  }
}
