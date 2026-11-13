"""AAF to Reaper - Windows launcher.

Double-click to choose one or more AAF files; each one is converted into
a REAPER project in a "<name> - Reaper" folder next to the AAF.
"""
import os
import sys
import subprocess
import traceback

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import tkinter as tk  # noqa: E402
from tkinter import filedialog, messagebox  # noqa: E402

LOG_DIR = os.path.join(os.environ.get('LOCALAPPDATA') or HERE, 'AAF to Reaper')
LOG_FILE = os.path.join(LOG_DIR, 'AAF to Reaper.log')


def log_write(text):
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(text + '\n')
    except OSError:
        pass


def open_in_reaper(rpp):
    try:
        os.startfile(rpp)  # opens with the program associated with .RPP (REAPER)
    except OSError:
        messagebox.showerror('AAF to Reaper',
                             'Could not open the project. Is REAPER installed?\n\n' + rpp)


def show_in_explorer(rpp):
    subprocess.Popen(['explorer', '/select,', os.path.normpath(rpp)])


def main():
    root = tk.Tk()
    root.withdraw()
    root.attributes('-topmost', True)
    ico = os.path.join(HERE, 'AAF to Reaper.ico')
    if os.path.isfile(ico):
        try:
            root.iconbitmap(default=ico)
        except tk.TclError:
            pass

    files = [a for a in sys.argv[1:] if a.lower().endswith('.aaf')]
    if not files:
        files = list(filedialog.askopenfilenames(
            parent=root, title='Choose AAF file(s) to convert to REAPER projects',
            filetypes=[('AAF files', '*.aaf *.AAF'), ('All files', '*.*')]))
    if not files:
        return 0

    try:
        import aaf2rpp
    except Exception:
        messagebox.showerror('AAF to Reaper', 'Could not load the converter:\n\n'
                             + traceback.format_exc())
        return 1

    from datetime import datetime
    log_write('==== %s ====' % datetime.now().isoformat(' ', 'seconds'))

    done = []
    lines = []
    for path in files:
        name = os.path.basename(path)
        root.config(cursor='watch')
        root.update()
        try:
            r = aaf2rpp.convert_file(path, log=log_write)
            done.append(r['rpp'])
            lines.append('%s\n  -> %s  (%d tracks, %d clips)'
                         % (name, os.path.basename(r['rpp']), r['tracks'], r['items']))
            for w in r['warnings']:
                lines.append('  warning: ' + w)
            log_write('%s -> %s' % (path, r['rpp']))
            for w in r['warnings']:
                log_write('  warning: ' + w)
        except Exception as e:  # noqa
            lines.append('%s\n  ERROR: %s' % (name, e))
            log_write('%s ERROR\n%s' % (path, traceback.format_exc()))
    root.config(cursor='')
    summary = '\n'.join(lines)

    if not done:
        messagebox.showerror('AAF to Reaper', 'Conversion failed.\n\n' + summary +
                             '\n\nDetails: ' + LOG_FILE)
        return 1

    # result dialog with three buttons
    dlg = tk.Toplevel(root)
    dlg.title('AAF to Reaper')
    dlg.attributes('-topmost', True)
    dlg.resizable(False, False)
    tk.Label(dlg, text=summary, justify='left', anchor='w', padx=16, pady=12,
             wraplength=560).pack(fill='x')
    bar = tk.Frame(dlg, padx=12, pady=10)
    bar.pack(fill='x')
    choice = {'v': None}

    def pick(v):
        choice['v'] = v
        dlg.destroy()

    tk.Button(bar, text='Open in REAPER', width=16, default='active',
              command=lambda: pick('open')).pack(side='right', padx=4)
    tk.Button(bar, text='Show in Explorer', width=16,
              command=lambda: pick('show')).pack(side='right', padx=4)
    tk.Button(bar, text='Done', width=10, command=lambda: pick(None)).pack(side='right', padx=4)
    dlg.bind('<Return>', lambda e: pick('open'))
    dlg.bind('<Escape>', lambda e: pick(None))
    dlg.protocol('WM_DELETE_WINDOW', lambda: pick(None))
    dlg.update_idletasks()
    x = (dlg.winfo_screenwidth() - dlg.winfo_width()) // 2
    y = (dlg.winfo_screenheight() - dlg.winfo_height()) // 3
    dlg.geometry('+%d+%d' % (x, y))
    dlg.focus_force()
    root.wait_window(dlg)

    for rpp in done:
        if choice['v'] == 'open':
            open_in_reaper(rpp)
        elif choice['v'] == 'show':
            show_in_explorer(rpp)
    root.destroy()
    return 0


if __name__ == '__main__':
    sys.exit(main())
