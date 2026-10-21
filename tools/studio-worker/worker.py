#!/usr/bin/env python3
"""
Сборщик роликов студии LoyalUP (19.10.2026).

Цикл: взять версию из очереди → скачать материалы → для каждого формата
собрать композицию HyperFrames и отрендерить MP4 → кадр-обложка FFmpeg →
отправить файл → «готово». Ошибка одного ролика не останавливает сборщик.

Настройка — переменные окружения:
  STUDIO_API_BASE      адрес общей схемы LoyalUP, например https://levelupapp.ru
  STUDIO_TOKEN_FILE    файл с токеном (0600), по умолчанию
                       ~/.config/loyalup-workstation/studio-worker.token
  HYPERFRAMES_BIN      путь к hyperframes
  GSAP_JS              путь к gsap.min.js
  STUDIO_WORK_DIR      рабочая папка (по умолчанию ~/.local/state/studio-worker)
  STUDIO_WORKER_NAME   имя сборщика в журнале (по умолчанию имя машины)

  worker.py            — работать в цикле
  worker.py --once     — взять одну версию (или выйти, если очередь пуста)
  worker.py --local job.json OUT_DIR — собрать из файла без сервера (проверка)
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import compose  # noqa: E402

HERE = Path(__file__).resolve().parent
API = os.environ.get('STUDIO_API_BASE', 'https://levelupapp.ru').rstrip('/')
TOKEN_FILE = Path(os.environ.get('STUDIO_TOKEN_FILE',
                                 Path.home() / '.config/loyalup-workstation/studio-worker.token'))
HF = os.environ.get('HYPERFRAMES_BIN',
                    '/home/developer/projects/Levon_Blog/tools/motion-studio/node_modules/.bin/hyperframes')
GSAP = os.environ.get('GSAP_JS',
                      '/home/developer/projects/Levon_Blog/tools/motion-studio/node_modules/gsap/dist/gsap.min.js')
WORK = Path(os.environ.get('STUDIO_WORK_DIR', Path.home() / '.local/state/studio-worker'))
NAME = os.environ.get('STUDIO_WORKER_NAME', socket.gethostname())
IDLE_SLEEP = 15
RENDER_TIMEOUT = 20 * 60
EXT = {'video': '.mp4', 'photo': '.jpg', 'music': '.mp3'}


def log(*a):
    print(time.strftime('%Y-%m-%d %H:%M:%S'), *a, flush=True)


class Api:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers['X-Studio-Worker-Token'] = TOKEN_FILE.read_text().strip()

    def claim(self):
        r = self.s.post(f'{API}/api/v1/studio-worker/claim/', json={'worker': NAME}, timeout=30)
        r.raise_for_status()
        return r.json().get('job')

    def _v(self, job, tail):
        return f"{API}/api/v1/studio-worker/{job['schema']}/{job['version_id']}/{tail}"

    def download(self, job, url_path, dest: Path):
        with self.s.get(API + url_path, stream=True, timeout=120) as r:
            r.raise_for_status()
            with open(dest, 'wb') as f:
                for chunk in r.iter_content(1 << 20):
                    f.write(chunk)

    def progress(self, job, stage, pct):
        try:
            self.s.post(self._v(job, 'progress/'), json={'stage': stage, 'progress': pct}, timeout=20)
        except requests.RequestException as e:
            log('progress failed', e)

    def output(self, job, fmt, mp4: Path, poster: Path | None, meta: dict):
        files = {'mp4': (mp4.name, open(mp4, 'rb'), 'video/mp4')}
        if poster and poster.exists():
            files['poster'] = (poster.name, open(poster, 'rb'), 'image/jpeg')
        try:
            r = self.s.post(self._v(job, 'output/'), data={'fmt': fmt, **meta}, files=files, timeout=600)
            r.raise_for_status()
        finally:
            for _n, fh, _t in files.values():
                fh.close()

    def done(self, job, error=''):
        r = self.s.post(self._v(job, 'done/'), json={'error': error} if error else {}, timeout=30)
        r.raise_for_status()


def probe(path: Path) -> dict:
    out = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries',
                          'stream=width,height:format=duration', '-of', 'json', str(path)],
                         capture_output=True, text=True, check=True).stdout
    data = json.loads(out)
    st = (data.get('streams') or [{}])[0]
    return {'width': st.get('width', 0), 'height': st.get('height', 0),
            'duration': round(float((data.get('format') or {}).get('duration') or 0), 2)}


def poster_of(mp4: Path, at: float) -> Path:
    dest = mp4.with_suffix('.jpg')
    subprocess.run(['ffmpeg', '-y', '-v', 'error', '-ss', f'{at:.2f}', '-i', str(mp4), '-frames:v', '1',
                    '-q:v', '3', str(dest)], check=True)
    return dest


def _ожидаемая_длина(job: dict, fmt: str) -> float | None:
    if fmt == '6s':
        return compose.SHORT_BODY + compose.SHORT_FINAL
    scn = job.get('scenario') or {}
    сцены = scn.get('scenes') or []
    if not сцены:
        return None
    return sum(float(x.get('length') or 0) for x in сцены) + float((scn.get('final') or {}).get('length') or 0)


def check(job: dict, fmt: str, mp4: Path, meta: dict, with_music: bool) -> list[str]:
    """«Проверка: звук, стыки, поля» (22.10.2026): размер кадра, длина, звук без
    перегруза, нет чёрных провалов (стыки) и чёрных полос по краям (поля).
    Пусто — всё в порядке; иначе — что не так, словами."""
    беды = []
    w, h = compose.FORMATS[fmt]
    if (meta.get('width'), meta.get('height')) != (w, h):
        беды.append(f'{fmt}: кадр {meta.get("width")}×{meta.get("height")} вместо {w}×{h}')
    ждём = _ожидаемая_длина(job, fmt)
    if ждём and abs(meta.get('duration', 0) - ждём) > 1.5:
        беды.append(f'{fmt}: длина {meta.get("duration")} с вместо ~{ждём:.1f} с')
    res = subprocess.run(['ffmpeg', '-hide_banner', '-nostats', '-i', str(mp4), '-vf',
                          'blackdetect=d=1.0:pix_th=0.03,cropdetect=limit=16:round=2:reset=0',
                          '-af', 'volumedetect', '-f', 'null', '-'],
                         capture_output=True, text=True, timeout=300)
    лог = res.stderr or ''
    if 'black_start' in лог:
        беды.append(f'{fmt}: чёрный провал на стыке дольше секунды')
    crops = [ln.rsplit('crop=', 1)[1].split()[0] for ln in лог.splitlines() if 'crop=' in ln]
    if crops:
        try:
            cw, ch = (int(x) for x in crops[-1].split(':')[:2])
            if cw < w * 0.9 or ch < h * 0.9:
                беды.append(f'{fmt}: чёрные поля по краям ({cw}×{ch} из {w}×{h})')
        except ValueError:
            pass
    пик = next((ln.split('max_volume:')[1].strip() for ln in лог.splitlines() if 'max_volume:' in ln), '')
    if with_music and not пик:
        беды.append(f'{fmt}: нет звука, хотя музыка в ролике есть')
    elif пик:
        try:
            if float(пик.split()[0]) > -0.1:
                беды.append(f'{fmt}: звук перегружен (пик {пик})')
        except ValueError:
            pass
    return беды


def prepare(job: dict, root: Path, fetch) -> dict:
    """Папка проекта: assets/, fonts/, gsap. fetch(url_path, dest) скачивает файл."""
    (root / 'assets').mkdir(parents=True, exist_ok=True)
    (root / 'fonts').mkdir(exist_ok=True)
    shutil.copy(GSAP, root / 'assets/gsap.min.js')
    shutil.copy(HERE / 'fonts/Manrope.ttf', root / 'fonts/Manrope.ttf')
    files = {}
    for a in job.get('assets') or []:
        ext = Path(a.get('name') or '').suffix.lower() or EXT.get(a['kind'], '')
        rel = f"assets/a{a['id']}{ext}"
        fetch(a['url'], root / rel)
        files[f"asset-{a['id']}"] = rel
    for key, url in (job.get('brand') or {}).items():
        rel = f'assets/{key}{".mp3" if key == "music" else ".png"}'
        try:
            fetch(url, root / rel)
            files[key] = rel
        except Exception as e:  # нет логотипа — ролик всё равно собирается
            log('brand file skipped', key, e)
    return files


def render_all(job: dict, root: Path, files: dict, report=lambda stage, pct: None) -> list[tuple]:
    """Рендер всех форматов → [(fmt, mp4, poster, meta)]."""
    out = []
    formats = job.get('formats') or list(compose.FORMATS)
    for i, fmt in enumerate(formats):
        report(f'Собираю {fmt.replace("x", ":")} ({i + 1} из {len(formats)})', 5 + int(90 * i / len(formats)))
        d = root / f'fmt-{fmt}'
        if d.exists():
            shutil.rmtree(d)
        shutil.copytree(root / 'assets', d / 'assets')
        shutil.copytree(root / 'fonts', d / 'fonts')
        (d / 'index.html').write_text(compose.build(job, fmt, files), encoding='utf-8')
        mp4 = root / f'{fmt}.mp4'
        res = subprocess.run([HF, 'render', '--quality', 'draft', '--workers', '1', '-o', str(mp4), str(d)],
                             capture_output=True, text=True, timeout=RENDER_TIMEOUT)
        if res.returncode != 0 or not mp4.exists():
            tail = (res.stderr or res.stdout or '').strip().splitlines()[-6:]
            raise RuntimeError(f'рендер {fmt} упал: ' + ' | '.join(tail))
        meta = probe(mp4)
        poster = poster_of(mp4, min(1.0, meta['duration'] / 2))
        out.append((fmt, mp4, poster, meta))
    return out


def run_job(api: Api, job: dict):
    root = WORK / f"{job['schema']}-v{job['version_id']}"
    if root.exists():
        shutil.rmtree(root)
    log('job', job['schema'], job['version_id'], job.get('title'))
    try:
        api.progress(job, 'Скачиваю материалы', 2)
        files = prepare(job, root, lambda url, dest: api.download(job, url, dest))
        results = render_all(job, root, files, lambda st, p: api.progress(job, st, p))
        api.progress(job, 'Проверяю: звук, стыки, поля', 93)
        музыка = 'music' in files and (job.get('scenario') or {}).get('music') != 'none'
        беды = [b for fmt, mp4, _p, meta in results for b in check(job, fmt, mp4, meta, музыка)]
        if беды:
            raise RuntimeError('проверка не прошла: ' + '; '.join(беды))
        api.progress(job, 'Отправляю файлы', 96)
        for fmt, mp4, poster, meta in results:
            api.output(job, fmt, mp4, poster, meta)
        api.done(job)
        log('done', job['version_id'])
    except Exception as e:
        log('failed', job['version_id'], e)
        try:
            api.done(job, str(e)[:1500] or 'ошибка сборщика')
        except Exception as e2:
            log('could not report failure', e2)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def local(job_file: str, out_dir: str):
    """Проверка без сервера: в job.json пути материалов — локальные файлы."""
    job = json.loads(Path(job_file).read_text())
    root = Path(out_dir)
    root.mkdir(parents=True, exist_ok=True)
    files = prepare(job, root, lambda url, dest: shutil.copy(url, dest))
    for fmt, mp4, _poster, meta in render_all(job, root, files, lambda st, p: log(st)):
        log(fmt, mp4, meta)


def main():
    if len(sys.argv) >= 2 and sys.argv[1] == '--local':
        return local(sys.argv[2], sys.argv[3])
    once = '--once' in sys.argv
    WORK.mkdir(parents=True, exist_ok=True)
    api = Api()
    log('studio worker', NAME, '→', API)
    while True:
        try:
            job = api.claim()
        except requests.RequestException as e:
            log('claim failed', e)
            job = None
            if once:
                return 1
        if job:
            run_job(api, job)
        elif once:
            return 0
        else:
            time.sleep(IDLE_SLEEP)
        if once:
            return 0


if __name__ == '__main__':
    sys.exit(main() or 0)
