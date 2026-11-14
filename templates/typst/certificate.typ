// Certificate: landscape letter, framed, centred.
#import "house.typ": *

#let certificate(data: none, brand: none) = {
  let k = tokens(brand)
  set document(title: data.title + " — " + data.recipient, author: brand.name)
  set page(paper: "us-letter", flipped: true, margin: 0.75in, fill: k.paper,
    background: place(center + horizon, rect(width: 100% - 0.9in, height: 100% - 0.9in, stroke: 1.5pt + k.primary,
      inset: 0pt, rect(width: 100% - 0.16in, height: 100% - 0.16in, stroke: 0.5pt + k.primary))))
  set text(font: k.serif, size: 12pt, fill: k.ink, lang: "en", hyphenate: false)
  set align(center)
  v(0.35in)
  if brand.at("logo", default: "") != "" { image("/brands/" + brand.logo, height: 0.55in, alt: brand.name + " logo") }
  v(0.2in)
  label(brand.name, brand, color: k.primary)
  v(0.25in)
  text(font: k.display, size: 22pt, weight: "semibold", fill: k.primary, data.title)
  v(0.25in)
  text(font: k.sans, size: 10pt, fill: k.muted, data.at("lead", default: "This certifies that"))
  v(0.08in)
  text(font: k.serif, size: 30pt, weight: "semibold", data.recipient)
  v(0.12in)
  block(width: 6.2in, md(data.at("body", default: "")))
  v(1fr)
  grid(columns: data.signers.len() * (2.6in,), column-gutter: 0.6in,
    ..data.signers.map(s => {
      line(length: 100%, stroke: 0.6pt + k.ink)
      v(-0.04in)
      text(font: k.sans, size: 10pt, weight: "semibold", s.name)
      linebreak()
      text(font: k.sans, size: 8.5pt, fill: k.muted, s.title)
    }))
  v(0.15in)
  text(font: k.sans, size: 8.5pt, fill: k.muted)[#data.date#if data.at("number", default: "") != "" [ · No. #data.number]]
  v(0.3in)
}
