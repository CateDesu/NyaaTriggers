# Death recap icons

`assets/recap_icons.zip` contains the action and status icon catalog and the game PNG files used by Death Recap. This includes the numbered icon variants for stacking statuses. Multiple actions and statuses can share an icon.

Status entries also include the game's description, beneficial or detrimental category, and permanence flag. These supply tooltips, the separate buff and debuff event filters, and status expiry handling. Permanent effects remain active until removal or an authoritative status list clears them. Numbered icons use the status's `MaxStacks` limit and the base icon plus the stack count minus one, matching [Death Recap's icon selection](https://github.com/Kouzukii/ffxiv-deathrecap/blob/658ec3a19614f225e354b207ebba87aaf64943c7/UI/DeathRecapWindow.cs).

The catalog comes from the [XIVAPI action and status sheets](https://v2.xivapi.com/docs/guides/sheets/). Images come from the game assets already included with Triggevent or from the [XIVAPI asset service](https://v2.xivapi.com/docs/guides/assets/). FINAL FANTASY XIV images and names belong to Square Enix. The archive records the XIVAPI game data version used for each sheet in `catalog.json`.

The archive ships with source and packaged builds. Recaps can display those icons offline. Unknown action or status IDs use asynchronous XIVAPI requests with a bounded disk cache and an in-memory image limit. Downloaded status metadata updates names and filters even when its image is unavailable. A failed download leaves the recorded event and tooltip available.

Refresh the catalog and icons after a game data update:

```sh
python3 tools/build_recap_assets.py
```

To reuse an existing Triggevent icon directory and download only missing images:

```sh
python3 tools/build_recap_assets.py --local-icons triggevent-core/event-trigger/xivdata/src/main/resources/xiv/icon
```

The archive includes 7,042 images, covering 4,794 status entries with icons and 49,630 action entries. The current and legacy asset services both lack icon 215049 for **Battle Efficiency Down**, so this one documented gap uses an unknown-icon indicator. Status rows whose game data specifies no icon are hidden from the icon strip.

The builder replaces the archive only after all other referenced icons are available. It does not change or rebuild Triggevent.
