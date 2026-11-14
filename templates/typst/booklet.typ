// Booklet: half-letter (5.5 × 8.5 in) reading pages; impose onto Letter with `pdfcore impose` for saddle stitching.
#import "house.typ": *

#let booklet(data: none, brand: none) = {
  let k = tokens(brand)
  set document(title: data.title, author: brand.name)
  set page(width: 5.5in, height: 8.5in, margin: (inside: 0.75in, outside: 0.6in, top: 0.7in, bottom: 0.75in), fill: k.paper,
    footer: context {
      let n = counter(page).get().first()
      if n > 1 {
        set text(font: k.sans, size: 8.5pt, fill: k.muted)
        if calc.even(n) { align(left)[#n · #data.title] } else { align(right)[#data.title · #n] }
      }
    })
  set text(font: k.serif, size: 10.5pt, fill: k.ink, lang: "en", hyphenate: false)
  set par(justify: false, leading: 0.7em, spacing: 1em)
  show heading.where(level: 1): it => { pagebreak(weak: true); block(below: 0.9em, text(font: k.sans, size: 14pt, weight: "semibold", fill: k.primary, it.body)) }
  show heading.where(level: 2): it => block(above: 1.3em, below: 0.6em, text(font: k.sans, size: 10pt, weight: "semibold", it.body))
  // Cover
  page(footer: none, margin: 0.6in, {
    v(1fr)
    if brand.at("logo", default: "") != "" { image("/brands/" + brand.logo, height: 0.5in, alt: brand.name + " logo"); v(0.2in) }
    text(font: k.display, size: 22pt, weight: "semibold", fill: k.ink, data.title)
    if data.at("subtitle", default: "") != "" { v(0.05in); text(font: k.sans, size: 11pt, fill: k.muted, data.subtitle) }
    v(0.3in)
    line(length: 30%, stroke: 1.5pt + k.primary)
    v(0.1in)
    text(font: k.sans, size: 8.5pt, fill: k.muted, brand.name + if data.at("date", default: none) != none { " · " + data.date } else { "" })
  })
  md(data.body)
}
