# Chezmoi Source File Naming Conventions

Chezmoi uses special prefixes and suffixes in source filenames to control how files are managed.
When mapping between source and destination paths, these prefixes/suffixes are stripped.

## Directory Prefixes

| Prefix        | Effect                                           |
|---------------|--------------------------------------------------|
| `dot_`        | Replaced with `.` in destination (e.g. `dot_config` → `.config`) |
| `exact_`      | Remove files in destination not present in source |
| `empty_`      | Ensure directory exists even if empty             |
| `encrypted_`  | Directory contents are encrypted                  |
| `external_`   | Directory contents pulled from external source    |
| `modify_`     | Run script to modify existing file                |
| `private_`    | Set permissions to 0700                           |
| `readonly_`   | Set permissions to 0555                           |
| `remove_`     | Remove the directory                              |
| `symlink_`    | Create a symlink instead of a regular directory   |

## File Prefixes

| Prefix         | Effect                                          |
|----------------|------------------------------------------------|
| `dot_`         | Replaced with `.` in destination               |
| `empty_`       | Ensure empty file exists                        |
| `encrypted_`   | File contents are encrypted                     |
| `executable_`  | Set executable bit (mode 0755)                  |
| `literal_`     | Don't interpret the filename as a template      |
| `modify_`      | Run as script to modify existing file           |
| `once_`        | Only create file once, don't update             |
| `private_`     | Set permissions to 0600                         |
| `readonly_`    | Set permissions to 0444                         |
| `remove_`      | Remove the file                                 |
| `run_`         | Run as a script                                 |
| `run_after_`   | Run as a script after other changes             |
| `run_before_`  | Run as a script before other changes            |
| `run_once_`    | Run script only once                            |
| `run_onchange_`| Run script when its contents change             |
| `symlink_`     | Create a symlink                                |
| `create_`      | Create file only if it doesn't exist            |

## File Suffixes

| Suffix    | Effect                                              |
|-----------|-----------------------------------------------------|
| `.tmpl`   | File is a Go template — processed with chezmoi data |
| `.literal`| Treat contents literally (no template processing)   |

## Prefix Ordering

When multiple prefixes are present, they must appear in this order:
`remove_`, `create_`, `modify_`, `run_`, `empty_`, `encrypted_`, `once_`, `private_`, `readonly_`, `exact_`, `executable_`, `symlink_`, `literal_`, `dot_`

## Examples

| Source path                              | Destination path             | Notes                        |
|------------------------------------------|------------------------------|------------------------------|
| `dot_bashrc`                             | `.bashrc`                    | Regular file                 |
| `dot_bashrc.tmpl`                        | `.bashrc`                    | Template                     |
| `executable_script.sh`                   | `script.sh`                  | Executable file              |
| `private_dot_ssh/config`                 | `.ssh/config`                | Private dir + regular file   |
| `symlink_dot_config/broot`              | `.config/broot` (symlink)    | Symlink prefix               |
| `dot_local/bin/executable_myscript.ps1`  | `.local/bin/myscript.ps1`    | Nested with executable       |
| `dot_gitconfig.tmpl`                     | `.gitconfig`                 | Template with dot_ prefix    |
| `readonly_OneDrive`                      | `OneDrive`                   | Read-only directory          |

## Template Files

Files ending in `.tmpl` are Go templates processed by chezmoi. They can use:
- `{{ .chezmoi.hostname }}` — machine hostname
- `{{ .chezmoi.os }}` — operating system
- `{{ .chezmoi.username }}` — current user
- `{{ if eq .chezmoi.hostname "myhost" }}...{{ end }}` — conditionals
- Custom data from `chezmoi.toml`'s `[data]` section

When comparing template source files to destination files, the source will contain template syntax
while the destination will have the rendered values. Use `chezmoi execute-template` or `chezmoi cat`
to see the rendered version of a template.
