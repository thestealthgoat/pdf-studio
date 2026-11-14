// Book manuscript / trade interior: 6 × 9 in, justified with hyphenation, chapters open on recto.
#import "house.typ": *

#let manuscript(data: none, brand: none) = {
  let k = tokens(brand)
  set document(title: data.title, author: data.author)
  set page(width: 6in, height: 9in, margin: (inside: 0.875in, outside: 0.625in, top: 0.75in, bottom: 0.8in), fill: k.paper,
    header: context {
      let n = counter(page).get().first()
      let chapter-start = query(heading.where(level: 1)).any(h => h.location().page() == n)
      if n > 2 and not chapter-start {
        set text(font: k.serif, size: 8.5pt, fill: k.muted, style: "italic")
        if calc.even(n) { align(left, data.author) } else { align(right, data.title) }
      }
    },
    footer: context {
      let n = counter(page).get().first()
      if n > 2 { align(center, text(font: k.serif, size: 8.5pt, fill: k.muted, str(n))) }
    })
  set text(font: k.serif, size: 11pt, fill: k.ink, lang: "en", hyphenate: true)
  set par(justify: true, leading: 0.68em, spacing: 0.68em, first-line-indent: (amount: 1.2em, all: false))
  show heading.where(level: 1): it => {
    pagebreak(to: "odd", weak: true)
    v(1.4in)
    align(center, text(font: k.serif, size: 14pt, weight: "semibold", it.body))
    v(0.5in)
  }
  // Title page
  page(header: none, footer: none, align(center, {
    v(2.2in)
    text(font: k.serif, size: 22pt, weight: "semibold", data.title)
    if data.at("subtitle", default: "") != "" { v(0.1in); text(font: k.serif, size: 12pt, style: "italic", data.subtitle) }
    v(0.6in)
    text(font: k.serif, size: 12pt, data.author)
  }))
  page(header: none, footer: none, align(bottom, text(font: k.serif, size: 8.5pt, fill: k.muted,
    [© #data.at("year", default: "") #data.author. All rights reserved.])))
  md(data.body)
}
