# README banner

`dopplergeist-banner.svg` is the self-contained source; the PNG is a native-size
preview. Keep the SVG filename stable because the root README embeds it.

The composition is adapted from the supplied `DOPPLERGEIST-README-refresh.zip`:
navy field, spectral traces and original aircraft silhouette, without visible
lettering. The two target circles share the aircraft silhouette's geometric
center. The 1400 × 289 canvas crops the former title area, leaving 53 SVG units
above and below the artwork. The illustration is not recorded data or an accuracy
claim.

## Illustrative model and maintenance

The dominant trace follows a constant-velocity pass at 79 m/s, a closest slant
range of 2,000 m and sound speed of 343 m/s. It plots reception time
`tau + range(tau)/c` against logarithmic observed frequency, with
`f/f0 = c/(c + d(range)/d(tau))`. The secondary tones share the same fractional
Doppler shift, with harmonic ratios 10:9:8. Frequency is higher on approach and
lower on recession, flattening at both ends. Faint irregular spectral strokes
and restrained energy changes suggest a measured signal without representing
one. These display parameters are independent of the scientific application.

Preserve the SVG title and description for accessibility. Recheck the curve,
circle alignment and balanced vertical margins at native resolution and 600 px
wide after any edit.

Render previews with librsvg:

```bash
rsvg-convert docs/assets/dopplergeist-banner.svg \
  -o docs/assets/dopplergeist-banner-preview.png
rsvg-convert -w 600 docs/assets/dopplergeist-banner.svg -o /tmp/dopplergeist-banner-600.png
```

## Livermore demonstration

`livermore-real-demo.png` shows a real recording and a configured search over
published flight data. It uses USGS / USDA NAIP imagery and data from Praveen
Pathak's [Zenodo dataset](https://doi.org/10.5281/zenodo.23424278) (CC BY 4.0).
[Setup, limitations and rendering instructions](../LIVERMORE_DEMO.md)
explain the inferred reference and the incomplete model assessment.

`livermore-automatic-mp3.png` depicts the separate automatic MP3 upload and
its retained aircraft-search scores, using the same USGS / USDA NAIP imagery.
The [all-aircraft case study](../LIVERMORE_DEMO.md#all-aircraft-archive-mp3-ranking)
describes the selected map points, inferred reference, and run limitations.
