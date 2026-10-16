# Lab 5 screenshot privacy and commit mapping

Date: 2026-10-16

I removed the top 40 pixels from four screenshots to hide the Azure account
header before publishing the Lab 5 commits. These are normal crops, not
AI-generated images. The approved crops are saved as PNG files so the remaining
pixels are unchanged. The four README image links now use `.png` instead of `.jpg`.

## Commit mapping

The seven local commits were based on published commit
`29cb75004965e123bb7c3c6e051ce0e56de69ec8`. The first four commits did not change.
The screenshot commit and its two descendants have new SHAs. Their messages,
author/committer metadata and original timestamps were preserved.

| Original SHA | Replacement SHA |
| --- | --- |
| `18f4ad4b20ea724831001dc4fb60835eca255dee` | `18f4ad4b20ea724831001dc4fb60835eca255dee` |
| `9766879e586090893b6b3ce1f4ffb427e868a180` | `9766879e586090893b6b3ce1f4ffb427e868a180` |
| `21cccefa98f99e65ebc0b1a4d959b42453305ef5` | `21cccefa98f99e65ebc0b1a4d959b42453305ef5` |
| `c58490cc91d9ec8de9b52651e9ef2fc63e510f61` | `c58490cc91d9ec8de9b52651e9ef2fc63e510f61` |
| `4bec26f1a3e52886da8a74e3a1f34d1bff9e8277` | `bc36cf080a2a362424510d7a50be7301361a9b02` |
| `4dfb85dd9ac43f7502f173cf7a6f7db0fd9e4b65` | `42fddcd38cd0b716f7d574771de1fbb5f7619a4f` |
| `aa6a4cdea8da1529fa0f70de529b782ed80c0a46` | `ef991a1bfe6a9272aa48821727519c5cd8c99f2a` |

## What this means for the existing evidence

The saved reports, image tags and Azure job records keep their original SHAs.
For example, the full pipeline run still records `aa6a4cd`, because that was the
code revision used for the run. I did not change it to `ef991a1` or claim a new run.

The published branch will not contain the three original rewritten commits.
Use this table to locate their replacements. Each replacement has exactly the
same application code, tests, configuration and other files as its original,
apart from the four screenshots and their README links. This mapping is not a
claim that an existing container image was rebuilt from the replacement SHA.

No image was rebuilt, no training or pipeline was rerun, and no Azure setting was
changed for this privacy edit.

## Screenshots

- [Three Lab 5 managed identities](screenshots/lab5-identities-2026-10-13.png)
- [Model Registrar permissions](screenshots/lab5-model-registrar-actions-2026-10-13.png)
- [Runtime Model Reader assignment](screenshots/lab5-runtime-model-reader-2026-10-13.png)
- [Runtime Model Reader permissions](screenshots/lab5-runtime-model-reader-actions-2026-10-13.png)

## Backup and verification

The original `main` history is kept in a private Git bundle outside the repository.
The bundle passed `git bundle verify` and contains the complete original history.
The files that were not yet committed were also backed up separately before the
rewrite. Ignored local configuration and environments were not included or changed.

The replacement history was checked commit by commit. The original four image
blobs are not reachable from the new `main`, and all other file content is
unchanged except for the four image-link extensions. Existing uncommitted work
was preserved.

Original objects can still exist in local reflogs and the private backup. This
was not a secure erase. Do not publish the backup or refs pointing to the old
history. No remote history was rewritten.
