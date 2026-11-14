// Report: title block, optional summary and contents, numbered headings, prose in the measure.
#import "house.typ": *

#let report(data: none, brand: none) = {
  let k = tokens(brand)
  show: doc.with(
    brand: brand,
    title: data.title,
    kind: data.at("kind", default: "Report"),
    author: data.at("author", default: ""),
    keywords: data.at("keywords", default: ()),
    date: data.at("date", default: none),
    rev: data.at("rev", default: none),
  )
  set heading(numbering: if data.at("numbered", default: true) { "1.1" } else { none })

  let facts = ()
  for (l, key) in (("Prepared for", "client"), ("Author", "author"), ("Date", "date"), ("Revision", "rev")) {
    let v = data.at(key, default: none)
    if v != none and v != "" { facts.push((l, str(v))) }
  }
  title-block(brand, kind: data.at("kind", default: "Report"), title: data.title, subtitle: data.at("subtitle", default: none), facts: facts)

  if data.at("summary", default: "") != "" {
    side("Summary", brand, md(data.summary))
    v(0.1in)
  }
  if data.at("toc", default: true) {
    side("Contents", brand, {
      set text(font: k.sans, size: 10pt)
      show outline.entry: it => link(it.element.location(), it.indented(it.prefix(), {
        it.body()
        box(width: 1fr, repeat(text(fill: k.rule, "·"), gap: 3pt))
        it.page()
      }))
      outline(title: none, depth: 2, indent: 1.2em)
    })
  }
  md(data.body)
}
