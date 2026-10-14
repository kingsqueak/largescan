# largescan

Mac-only terminal browser for large folders, large files, and Library data left behind by apps that are no longer installed. It never deletes anything.

## What it shows

One scan fills three scrollable tabs.

**Folders.** Every folder larger than 1 GB (1,073,741,824 bytes), largest first. A folder's size is the total size of the files inside it, including its subfolders. The folder you scan is not in this list. Its total is on the summary line and in the header.

**Files.** Every file larger than 1 MB (1,048,576 bytes), largest first.

**Orphans.** Data under `~/Library` whose app is no longer installed. This tab always reads your Library, even when the path box points somewhere else.

- **Abandoned.** A bundle identifier with no installed app.
- **Possible leftover.** A folder name that matches no installed app. A shared vendor folder can outlive one app.
- **Missing helper.** A Launch Agent whose program file is gone.

Sizes use 1024-based units. Over 1 MB is cyan, over 100 MB is yellow, and over 1 GB is red. The scan does not follow symbolic links.

## Install

largescan needs Python 3.9 or newer and runs on macOS only.

```bash
brew install pipx
pipx install git+https://github.com/kingsqueak/largescan.git
```

Then run:

```bash
largescan
largescan ~/Downloads
```

A path on the command line fills the path box. Press Enter or Scan to run. Press `?` or Help to open the help file.

To update later:

```bash
pipx upgrade largescan
```

### From a checkout

```bash
git clone https://github.com/kingsqueak/largescan.git
cd largescan
pipx install --editable .
```

Edits in the checkout apply without reinstalling.

To run the checkout without installing the command:

```bash
pip3 install -r requirements.txt
python3 largescan.py
```
