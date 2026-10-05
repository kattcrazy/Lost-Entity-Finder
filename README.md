<!-- project-directory-status -->
<div align="center">

[![Maintained (low priority)](https://img.shields.io/endpoint?style=for-the-badge&url=https%3A%2F%2Fraw.githubusercontent.com%2Fkattcrazy%2Fkattcrazy%2Fmain%2Fbadges%2FLost-Entity-Finder.json)](https://github.com/kattcrazy/kattcrazy/blob/main/PROJECT-DIRECTORY.md) [![Up for adoption - credit required](https://img.shields.io/endpoint?style=for-the-badge&url=https%3A%2F%2Fraw.githubusercontent.com%2Fkattcrazy%2Fkattcrazy%2Fmain%2Fbadges%2FLost-Entity-Finder-section.json)](https://github.com/kattcrazy/kattcrazy/blob/main/PROJECT-DIRECTORY.md)

</div>
<!-- /project-directory-status -->

## <img src="custom_components/lost_entity_finder/brand/icon.png" alt="Lost Entity Finder icon" width="36" /> Lost Entity Finder

Detect lost entity references after entity ID changes in Home Assistant. When you change an entity ID (for example `sensor.door` -> `sensor.window`), Lost Entity Finder checks automations, scripts, scenes, dashboards, and YAML from `configuration.yaml` (packages, included directories, and themes). When it finds stale references, it raises one repair per changed entity ID with direct links to each location, along with options to ignore or auto-replace in bulk.

> Use with caution! This integration is basically a best-effort giant find-and-replace (if enabled) and can make mistakes. Please back up your Home Assistant instance before using for many entities.

<img width="402"  alt="image" src="https://github.com/user-attachments/assets/98351145-6ec8-46fd-9057-d2b98d69a7f9" />
<img width="382" alt="image" src="https://github.com/user-attachments/assets/eb268543-cb18-464d-80ce-4c96b8d5f6b8" />

> Please note, Lost Entity Finder only handles entity ID changes, including resets. It does not handle deleted entities or unavailable entities. In addition, it cannot check your ESP devices, or any other third party thing that relies on your HA entities.

## Installation

### HACS custom Repo (recommended)

1. Add `https://github.com/kattcrazy/Lost-Entity-Finder` as a custom repository in HACS (category: Integration)
2. Search for Lost Entity Finder & click Download
3. Restart Home Assistant
4. Add the integration under Settings -> Devices & services

### Manual

1. Copy the `custom_components/lost_entity_finder` folder into your Home Assistant `custom_components` directory
2. Restart Home Assistant
3. Add the integration under Settings -> Devices & services

## Configuration & Features

### Auto-Replace

On setup you can enable Auto-Replace (bulk fix). Default is off. Change this anytime via Settings -> Devices & services -> Lost Entity Finder -> Configure.

You can also set the maximum number of tracked entity ID changes (default 1000, allowed range 50 to 5000). Raise this if you bulk-rename many entities at once.

Auto-Replace is offered for automations, scripts, scenes, and dashboards that Home Assistant stores in the UI files (`automations.yaml`, `scripts.yaml`, `scenes.yaml`) or in dashboard storage. Blueprint-backed UI automations and scripts are listed for a manual edit. Other YAML, including packages and themes, is listed in the repair and has to be updated by hand. **Auto-Replace All Lost Entity Repairs** skips repairs that are manual-only.

Auto-Replace makes a backup of each UI yaml file next to itself (filename +  `.pre-lost-entity-finder`) before editing. It then does the auto-replace, and restores the copy if the file was changed during the edit or the edit was corrupted/touched more than it should have.

### Entities

Lost Entity Finder adds the following entities:

| Entity | Type | Description |
|--------|------|-------------|
| Rescan for Lost Entity Repairs | Button | Rescan for lost entity references and sync repairs |
| Ignore All Lost Entity Repairs | Button | Ignore all active lost entity repairs |
| Restore Ignored Lost Entity Repairs | Button | Clear all ignored lost entity repairs and rescan |
| Auto-Replace All Lost Entity Repairs | Button | Replace all auto-fixable lost entity references (only created if Auto-Replace is enabled) |

### Repairs

After an entity ID change, open Settings -> System -> Repairs. A repair appears only when stale references are found (unless you use `check_entity_id_pair` with `create_repair_without_references`). Each repair lists all locations that still reference that old entity ID.

When Auto-Replace is enabled, Auto-Replace is the default action. If some references can be updated automatically and others cannot, the repair runs Auto-Replace first, then shows the remaining manual locations with Fix later (default), Mark as completed, or Ignore. Manual-only repairs offer the same choices. Fix later closes the dialog and leaves the repair in the list until you finish the manual updates or pick another option.

### Services

Use `lost_entity_finder.find_entity_references` to scan for a specific entity ID on demand. Raises a repair under Settings -> System -> Repairs with links to each location. This does not offer Auto-Replace (no old-to-new pair).

Example

```yaml
service: lost_entity_finder.find_entity_references
data:
  entity_id: light.name
```

Use `lost_entity_finder.check_entity_id_pair` to check one or more old-to-new entity ID pairs and sync repairs. Pass pairs in `renames`. Set `create_repair_without_references` to create a repair even when no stale references are found.

Example

```yaml
service: lost_entity_finder.check_entity_id_pair
data:
  renames:
    light.old_name: light.new_name
    sensor.old_name: sensor.new_name
  create_repair_without_references: false
```

## License

This project uses the [GNU General Public License v3.0](https://www.gnu.org/licenses/gpl-3.0.html). See [LICENSE](LICENSE) for the full legal text. In short: you can use, change, and share it freely. If you distribute a modified version, you must offer it under the same license and share the source too, so the work (and its derivatives) stay open. You cannot take this code, tweak it, and ship it as a closed product.

## About
Hope this helps! Built to solve my own problem ;)

If this helps you out a heap as I'm sure it will, consider supporting me [here](https://kattcrazy.nz/product/support-me/) :)
