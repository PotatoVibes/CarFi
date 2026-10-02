# Special editions — catalog, evidence, and rules

Researched and corpus-audited 2026-10-02. Code: `src/carfinder/editions.py`.
Scoring: a detected edition adds a flat **+5** (`scorer.EDITION_BONUS`) on top of
the capped highlight bonus, so totals now run 0–120. It's kept out of
`highlight_bonus`, so it never pushes a car over the unicorn bar. The badge (a gold
medal next to the score) shows on every board row with an edition, not only
ranked ones: unlike the unicorn bar, it's a fact about the car.

**Corpus** = read-only snapshot of the live DB: 1,083 parsed window stickers,
649 gmpartsgiant build sheets and 15,658 listing rows across Auto.dev,
Marketcheck and manual sources.

## How a car is identified

Each catalog rule is first gated by a **VIN scope**: model line plus model year
(VIN position 10). GM reuses option codes across years, CT4 `ZL*` codes are
generic "designer theme" slots, and phrases like "IMSA" or "Launch Edition"
belong to other models too. Inside the scope, evidence is checked in this order:

| # | Evidence | Provenance |
|---|---|---|
| 1 | Edition RPO code on the window sticker or build sheet | verified |
| 2 | Edition line in the window-sticker text | verified |
| 3 | VIN in the edition's factory serial block | verified |
| 4 | Complete sticker with none of the above → **not an edition**; listing claims can't override it | verified "no" |
| 5 | Listing headline text (see below) | claimed |

A claimed edition still earns the +5 and the medal, rendered dimmed with
"claimed" in the tooltip. Claimed highlights get the same treatment: claimed
evidence may raise a score but never satisfies a hard requirement. Edition text
is a separate input (`Vehicle.listing_text`), so it can't move any hard or
highlight matcher.

**Listing headline text** is collected from *every* source's listing row, not
the merged `vehicles` row, which is last-writer-wins. Before this change,
Auto.dev's "3.0" overwrote a Marketcheck "A91-MT Edition". Fields read:

- Marketcheck: heading, `build.trim`, `build.version`, `vdp_url`, exterior color.
- Auto.dev: `vehicle.trim` (GM makes only), exterior color, VDP URL.
- Manual: the URL.

Fields never read:

- Dealer name, address and city ("Alan Jay Cadillac of **Sebring**", "**Burnout**
  Garage", "2101 **45th** Street").
- Photo URLs (hex hashes).
- Pasted page text (similar-vehicle widgets name other cars).
- Auto.dev's trim for BMW/Toyota. It's a decoder default: every 2026 Supra reads
  "MkV Final Edition", proven wrong on a real 3.0 Premium.

## Catalog

### C8 Corvette Stingray. Scope: 3LT (VIN pos 5 `C`) with the LT2

| Edition | Year | Built | Verified signal | Corpus |
|---|---|---|---|---|
| IMSA GTLM Championship C8.R Edition | 2022 | 1,000 (470 coupe / 530 conv; 660 Hypersonic Gray, 340 Accelerate Yellow) | RPO **ZCR**; sticker "IMSA GTLM Championship C8.R Edition" | 5 build sheets, all ZCR; 0 other 2022s carry it |
| 70th Anniversary Edition | 2023 | 4,579 Stingrays | RPO **Y70**; sticker "70TH ANNIVERSARY SPECIAL"; VIN serial `P5 50xxxx` | 31 Y70 Stingrays; 31/31 with factory data in the serial block are Y70, 0 outside it |
| Stars & Steel Limited Edition | 2026 | 250 across the whole C8 line | RPO **USA**; sticker "STARS AND STEEL" | 1 sticker, but it's an E-Ray (out of scope); no Stingray yet |

Sources: Chevrolet newsroom (2021-06-09), GM Authority, CorvetteActionCenter
pricing (ZCR $6,595), CorvetteBlogger production stats, CorvetteForum 70th VIN
sequence thread, GM Authority / CorvetteForum Stars & Steel pricing (USA $9,995).

### CT4-V Blackwing. Scope: VIN pos 8 `P` (LF4)

Cadillac numbers these editions in VIN positions 12–17: `5`, then `1` (auto) or
`6` (manual), then the edition digit, then the plaque number. This matches the
GM-published Mondrian plaque format `4-5[1|6][1|2|3][001-066]`. In the corpus,
33 of 33 edition-block VINs are editions and every regular CT4 has position 12 =
`1`. Because the block is complete and definitive, **these rules have no
listing-text path**: a claim on a car outside the block can only be wrong.

| Edition | Year | Built | RPO | Serial block | Sticker line |
|---|---|---|---|---|---|
| **Watkins Glen IMSA Edition** | 2023 | 99 (Electric Blue) | ZLE | `P0 5[16]1 0nn` | "WATKINS GLEN IMSA EDITION 19,200.00" |
| Sebring IMSA Edition | 2023 | 99 (Maverick Noir Frost) | ZLD | `P0 5[16]6 0nn` | "SEBRING IMSA EDITION 22,500.00" |
| Road Atlanta IMSA Edition | 2023 | 99 (Rift Metallic) | ZLG | `P0 5[16]8 0nn` | "ROAD ATLANTA IMSA EDITION 19,200.00" |
| Arrival Edition (Mondrian) | 2024 | 66 (Mercury Silver) | ZLK | `R0 5[16]1 001–066` | "CT4-V BLACKWING ARRIVAL EDITION" |
| Impact Edition (Mondrian) | 2024 | 66 (Black Raven) | ZLJ | `R0 5[16]2 001–066` | "CT4-V BLACKWING IMPACT EDITION" |
| Elevation Edition (Mondrian) | 2024 | 66 (Velocity Red) | ZLR | `R0 5[16]3 001–066` | "CT4-V BLACKWING ELEVATION" (EDITION wraps) |
| Petit Pataud Edition | 2025 | 50 (Magnus Metal Frost) | ZLZ | `S0 5[16]0 001–050` | "Petit Pataud Special Edition" |

The owner's "IMSA" and "Watkins Glen" editions are this table's 2023 track
trio, plus the separate 2022 C8 IMSA GTLM car above. There is no Corvette
Watkins Glen edition.

Corpus: ZLE 9 VINs, ZLD 7, ZLG 4, ZLK 2, ZLJ 2, ZLR 5; ZLZ has 0, backed by two
external sources plus a replica window sticker. Sources: GM Authority edition
and production articles (2022-07/08, 2023-06/07, 2024-06, 2025-03), Carscoops,
Cadillac Society, Barrett-Jackson, and classic.com VIN pages (8 external VINs
fit the serial rule).

### BMW M2 (G87). Claimed only

| Edition | Year | Built | Rule |
|---|---|---|---|
| Turbo Design Edition | 2026–27 | ~300 US (unofficial) | Listing text "M2 Turbo Design / Edition", "M2 TDE" or "Turbo Design Edition"; VIN `3MF[24]3DM0` + year T/V |

Why it's claimed only:

- BMW has no free window sticker.
- Real Turbo Design Edition VINs decode as a plain 2026 M2 in NHTSA vPIC.
- Serials interleave with regular production.
- Bare "turbo" never counts: every S58 description has it.

**Note:** the Turbo Design Edition lists at $84k+, above the profile's price
hard bound, so a real one lands *excluded* (badge visible with "show excluded").

### Toyota GR Supra. Claimed only

| Edition | Year | Built | Listing-text rule |
|---|---|---|---|
| Launch Edition | 2020 | 1,500 | "Launch Edition" (VIN `WZ1DB4C`, L) |
| A91 Edition | 2021 | 1,000 | "A91 Edition", Refraction paint |
| A91-CF Edition | 2022 | 600 | "A91-CF" |
| **A91-MT Edition** | 2023 | 500, manual only | "A91-MT", CU Later Gray / Burnout paint |
| 45th Anniversary Edition | 2024 | 900 | "45th Anniversary", Mikan Blast paint |
| MkV Final Edition | 2026 | ~1,300 | "MkV Final", "Final Edition", "GT4-Style", Burnout / Undercover paint |

Why it's claimed only:

- Toyota has no free window sticker.
- The toyotapartsdeal catalog collapses every 3.0 into one entry.
- Edition serials interleave with regular production.

Paint names count only because Toyota's product-info sheets list them as
edition-exclusive *for that model year*.

Corpus: A91-MT 2 VINs (Marketcheck `build.trim`), 45th 5 VINs, 0 genuine Final
Editions. The 3 Auto.dev "MkV Final Edition" trims are decoder defaults. The
2020–22 editions are unreachable under the profile's 2023+ gate and are
cataloged for completeness.

These replace the old `gr_supra.yaml` highlights ("A91-MT Edition" +5, "MkV
Final Edition" +4, "45th Anniversary Edition" +3). Those stacked with the edition
bonus and matched the polluted merged trim, so they're removed from the repo
profile. **If they're still in your live config, delete them in the config UI.**

## Rejected lookalikes

These look like editions but aren't, so they never earn the badge:

- **Standard content on every car of a year:**
  - 2024 "20TH ANNIVERSARY OF V-SERIES COMMEMORATIVE CONTENT", on 38/38 Blackwings.
  - 2023 70th badging, rear-glass etching and dedication plaque.
  - 2023 CFY "dedication plaque", on 388 ordinary Stingrays.
- **Accessories and dealer add-ons:**
  - The $460 "70th Anniversary indoor car cover".
  - The C8.R-rendered car cover (WL0/WKR).
  - "Cadillac Edition first aid" kits.
  - Jake C8.R decal packages.
  - LPO "Stingray R" appearance packages.
- **Edition paint or parts on regular cars:**
  - Electric Blue is on 8 regular 2023 Blackwings and Rift on 18.
  - Black Raven plus both carbon packages appears without ZLJ.
  - Accelerate Yellow plus the Strike Yellow interior appears without ZCR.
  - CFZ/CFV, 709 and 5Z\* caps are all individually orderable.
  - Paint never triggers on its own.
- **Other model lines:**
  - Z06 / E-Ray / ZR1 cars carrying Y70 or USA.
  - ZR1X Quail Silver (ZRA).
  - 2027 Grand Sport Launch Edition (Z25).
  - CT5-V-only Blackwing editions (Le Monstre, 20th Anniversary ZLT, Precision Package).
  - M3/M4 "50 Jahre" and the M2 CS, which is a variant, not an edition.
- **Not US-market:**
  - Japan-only C8 editions (CERV I, Heritage, Yellow Jacket, Red Flame).
  - M2 30 Years (Mexico), RR Edition (South Africa), CS Edition Edge (Japan).
  - Korea online editions.
  - Supra Jarama / Fuji / Plasma Orange (Europe or Japan).
- **Not deterministic:**
  - The 2022 Blackwing "Collector Series", the first 250 reservation builds.
    RPO R6Q is on non-Collector cars too, and the registry operator says there
    is no way to tell from codes or sticker.

## Known gaps / watch items

- **Rules with no corpus example yet:**
  - The ZCR sticker wording, taken from a Hagerty transcription; the 5 ZCR cars
    have build sheets only.
  - A Stars & Steel Stingray.
  - Petit Pataud.
  - A real Turbo Design Edition or Supra Final Edition.

  Confirm when the first one syncs.
- **70th cars outside the serial block:** about 9 70th Stingrays were built on
  pilot VINs. Only Y70 on a sticker or build sheet catches them.
- **A likely A91-MT that doesn't fire:** WZ1DB0G08PW055841 is listed "Gray" by
  Auto.dev, and gray was A91-MT-only in 2023. The rule ignores generic "Gray",
  so it isn't badged; confirm by hand.
- **Future editions:** the CT4 and Supra lines are closed. No 2027 Stingray
  edition is announced; a rumored further G87 M2 variant is unannounced.
- **Applying to existing cars:** run **Rescore** (or `carfinder rescore`) after
  deploying so existing cars pick up badges and +5. The first rescore can fire
  threshold-crossing notifications for edition cars that move up past
  `NOTIFY_THRESHOLD`.
