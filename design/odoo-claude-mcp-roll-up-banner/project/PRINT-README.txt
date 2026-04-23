PRINT PACKAGE — Odoo × Claude × MCP         (PREPRESS-READY)
BL Consulting / Rosen Vladimirov
================================================================

СЪДЪРЖАНИЕ
----------
1) ROLL-UP BANNER                  trim 850 × 2000 mm   portrait
                                   file 856 × 2006 mm   (+ 3 mm bleed)
2) A5 FLYER (двустранен)           trim 148 × 210 mm    portrait
                                   file 152 × 214 mm    (+ 2 mm bleed)
3) Assets                          QR + portrait

================================================================
1. ROLL-UP BANNER — trim 850 × 2000 mm
================================================================
File:   Roll-up Banner - Print.html
        Chrome → Save as PDF → Page size: CUSTOM 856×2006 mm
                               Margins: None
                               Background graphics: ON
                               Scale: 100 %

Type:         Roll-up banner (алуминиева касета)
Trim:         850 × 2000 mm
Bleed:        3 mm всички страни  →  file 856 × 2006 mm
Safe area:    30 mm от trim (съдържанието е на 40–64 mm сигурно вътре)
Bottom hide:  ~100 mm (скрити от касетата — без важен контент долу!)

================================================================
2. A5 FLYER — trim 148 × 210 mm, 2 страници, двустранен
================================================================
File:   A5 Flyer - Print (Bleed 152x214).html
        Chrome → Save as PDF → Page size: CUSTOM 152×214 mm
                               Margins: None
                               Background graphics: ON
                               Scale: 100 %

Lice (page 1):  Английски
Grab (page 2):  Български
Paper:          Двустранен, 250–300 gsm matte/uncoated
Trim:           148 × 210 mm (реалният формат A5)
Bleed:          2 mm всички страни  →  file 152 × 214 mm
Safe area:      10 mm от trim (съдържанието седи в 128 × 190 mm)

================================================================
ИЗИСКВАНИЯ НА ПЕЧАТНИЦАТА ЗА A5 ФЛАЕРА
================================================================
Приемани типове:     .jpg .jpeg .tiff .tif .pdf
Максимален размер:   100 MB
Размер на макета:    152 × 214 mm  ← това е file-size с bleed
Разделителна:        300 DPI
Цветово пространство: CMYK (ISO Coated v2 или FOGRA51 препоръчително)

Сега работим в HTML/CSS → PDF експортът от Chrome е **RGB**.
За CMYK конверсия използвайте един от тези инструменти:

  Опция A (безплатно, онлайн):
    https://cloudconvert.com/pdf-to-pdf → Output → CMYK
    https://www.rgb2cmyk.org/ (за JPG/TIFF растер)

  Опция B (Acrobat Pro):
    File → Properties → Advanced → PDF/X Compliance
    Print Production → Convert Colors → CMYK (ISO Coated v2)

  Опция C (Ghostscript команден ред, CMYK + 300 DPI):
    gs -dSAFER -dBATCH -dNOPAUSE \
       -sDEVICE=pdfwrite \
       -sColorConversionStrategy=CMYK \
       -dProcessColorModel=/DeviceCMYK \
       -dPDFSETTINGS=/prepress \
       -sOutputFile=a5-flyer-cmyk.pdf a5-flyer.pdf

Опция D (растер 300 DPI CMYK JPG/TIFF):
    # първо RGB PDF → TIFF 300 DPI
    gs -dSAFER -dBATCH -dNOPAUSE -sDEVICE=tiff32nc \
       -r300 -sOutputFile=a5-flyer.tif a5-flyer.pdf
    # TIFF RGB → TIFF CMYK (ImageMagick)
    convert a5-flyer.tif -profile sRGB.icc \
      -profile USWebCoatedSWOP.icc -colorspace CMYK \
      -density 300 a5-flyer-cmyk.tif

================================================================
ЦВЕТОВЕ — еднакви за двата материала
================================================================
Accent (Claude Blue):
  oklch(0.55 0.17 250)  ≈  #4C6DF5
  Pantone approx:       2728 C
  CMYK approx:          C 85  M 65  Y 0  K 0
  RGB approx:           76 / 109 / 245

Paper (warm off-white):
  oklch(0.968 0.012 85) ≈  #F7F1E6
  Pantone approx:       Warm Gray 1 C / 9043 C (uncoated)
  CMYK approx:          C 2  M 4  Y 8  K 0

Ink (deep warm black):
  oklch(0.18 0.012 60)  ≈  #2A2118
  CMYK approx (rich):   C 60  M 65  Y 70  K 70

→ RIP / prepress: sRGB → CMYK с ISO Coated v2 или FOGRA51 ICC.

================================================================
ТИПОГРАФИЯ
================================================================
Instrument Serif  — заглавия, курсиви акценти
JetBrains Mono    — monospace, команди, labels
Geist             — body текст, UI elements

Всички са безплатни Google Fonts. Chrome при Save as PDF
ги embed-ва автоматично. Ако печатницата иска outlined PDF,
отворете PDF-а в Acrobat → Print Production → Flatten Transparencies.

================================================================
ФИНИШИРАНЕ
================================================================
Roll-up:  440 gsm blockout PVC ИЛИ matte polyester
          Solvent / latex / UV печат, 1200 dpi
          Матово ламинирано (опционално, срещу отблясъци)

A5:       250–300 gsm uncoated/matte карton
          Двустранно, 4/4 CMYK
          Без ламинат (по-ръкописно усещане)
          Опция: soft-touch matte за премиум

================================================================
PREPRESS QA ЧЕКЛИСТ (преди качване към печатница)
================================================================
[ ] PDF размер на страницата: точно 152×214 mm (A5) / 856×2006 mm (roll-up)
[ ] Няма бял ръб по която и да е страна (bleed пълни до ръба)
[ ] Текст/важен контент на ≥10 mm (A5) / ≥30 mm (roll-up) от trim
[ ] Roll-up: никакъв важен контент в последните 100 mm от долу (касета)
[ ] CMYK цветово пространство (или ICC profile embedded)
[ ] 300 DPI (растер) / vector запазен (PDF)
[ ] Шрифтове embed-нати или outlined
[ ] Файл под 100 MB

================================================================
КОНТАКТ ЗА ВЪПРОСИ
================================================================
Rosen Vladimirov — BL Consulting
vladimirov.rosen@gmail.com
https://bl-consulting.net
