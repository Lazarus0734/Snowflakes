import sys
import math
import random
import ctypes

from PyQt5.QtWidgets import QApplication, QWidget, QSystemTrayIcon, QMenu, QAction
from PyQt5.QtCore import Qt, QTimer, QPoint
from PyQt5.QtGui import QPainter, QColor, QIcon, QPixmap

try:
    import win32gui
    HAS_WIN32 = True
except ImportError:
    HAS_WIN32 = False

# DPI-awareness
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

user32 = ctypes.windll.user32

GWL_EXSTYLE = -20
WS_EX_TRANSPARENT = 0x20
WS_EX_LAYERED = 0x80000
LWA_ALPHA = 0x2
GA_ROOT = 2


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


IGNORED_CLASSES = {
    'Progman', 'WorkerW', 'Shell_TrayWnd', 'Shell_SecondaryTrayWnd',
    'Windows.UI.Core.CoreWindow', 'DV2ControlHost', 'ForegroundStaging',
    'SysShadow', 'Button', 'Static', 'TaskListThumbnailWnd',
}


def top_window_at(x, y, exclude=0):
    """Верхнее окно в точке (x, y). Пропускает наш оверлей."""
    if not HAS_WIN32:
        return 0
    try:
        pt = POINT(int(x), int(y))
        hwnd = user32.WindowFromPoint(pt)
        if not hwnd:
            return 0
        root = user32.GetAncestor(hwnd, GA_ROOT) or hwnd
        if root == exclude:
            return 0
        return root
    except Exception:
        return 0


class Flake:
    __slots__ = ("x", "y", "vx", "vy", "r", "gravity", "life")

    def __init__(self, x, y, vx=0.0, vy=0.0, r=2.0, gravity=0.01, life=None):
        self.x, self.y = x, y
        self.vx, self.vy = vx, vy
        self.r = r
        self.gravity = gravity
        self.life = life


class SnowOverlay(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating)

        geom = QApplication.primaryScreen().geometry()
        self.setGeometry(geom)
        self.screen_w = geom.width()
        self.screen_h = geom.height()

        try:
            self.dpi = self.screen_w / ctypes.windll.user32.GetSystemMetrics(0)
        except Exception:
            self.dpi = 1.0

        self.my_hwnd = 0  # будет установлен после winId()

        self.flakes = []
        self.landed = []
        self.window_rects = []
        self.play_mode = False
        self.grabbing = False
        self.grab_pos = QPoint(0, 0)
        self.grab_charge = 0
        self.ground_y = self.screen_h

        self.update_ground()
        self.update_windows()
        try:
            self.my_hwnd = int(self.winId())
        except Exception:
            pass

        # ~60 fps физика/рендер
        self.timer = QTimer(self); self.timer.timeout.connect(self.tick); self.timer.start(16)
        # обновление окон + отклеивание снега
        self.win_timer = QTimer(self); self.win_timer.timeout.connect(self.refresh_windows); self.win_timer.start(400)
        # панель задач
        self.ground_timer = QTimer(self); self.ground_timer.timeout.connect(self.update_ground); self.ground_timer.start(3000)

    # ---------- геометрия ----------
    def update_ground(self):
        self.ground_y = self.screen_h
        if not HAS_WIN32:
            return
        try:
            hwnd = win32gui.FindWindow("Shell_TrayWnd", None)
            if hwnd:
                l, t, r, b = win32gui.GetWindowRect(hwnd)
                t *= self.dpi; b *= self.dpi
                if b >= self.screen_h - 5 and t < self.screen_h - 20:
                    self.ground_y = t
        except Exception:
            pass

    def _is_bad_window(self, hwnd):
        try:
            cls = win32gui.GetClassName(hwnd)
            if cls in IGNORED_CLASSES:
                return True
        except Exception:
            return True
        return False

    def update_windows(self):
        rects = []
        if HAS_WIN32:
            dpi = self.dpi
            my = self.my_hwnd

            def cb(hwnd, _):
                if hwnd == my:
                    return True
                try:
                    if not win32gui.IsWindowVisible(hwnd) or win32gui.IsIconic(hwnd):
                        return True
                    if not win32gui.GetWindowText(hwnd):
                        return True
                    if self._is_bad_window(hwnd):
                        return True
                    # пропускаем полностью прозрачные layered-окна
                    ex = win32gui.GetWindowLong(hwnd, GWL_EXSTYLE)
                    if ex & WS_EX_LAYERED:
                        try:
                            color, alpha, flags = win32gui.GetLayeredWindowAttributes(hwnd)
                            if (flags & LWA_ALPHA) and alpha == 0:
                                return True
                        except Exception:
                            pass
                    l, t, r, b = win32gui.GetWindowRect(hwnd)
                    l, t, r, b = l * dpi, t * dpi, r * dpi, b * dpi
                    if r - l > 100 and b - t > 100 and t > 0:
                        rects.append((l, t, r, b))
                except Exception:
                    pass
                return True

            try:
                win32gui.EnumWindows(cb, None)
            except Exception:
                pass
        self.window_rects = rects

    def refresh_windows(self):
        """Обновить окна и сразу отклеить снег, который больше ни на что не опирается."""
        self.update_windows()
        self.unstick_landed()

    # ---------- отклеивание снега ----------
    def unstick_landed(self):
        if not self.landed:
            return
        surviving = []
        ground = self.ground_y
        my = self.my_hwnd
        rects = self.window_rects

        for f in self.landed:
            supported = False

            # лежит на панели задач / нижней границе экрана?
            if abs((f.y + f.r) - ground) < 3:
                supported = True
            else:
                # совпадает ли с верхней гранью какого-то окна в списке?
                rect_match = False
                for (l, t, r, b) in rects:
                    if l - 3 <= f.x <= r + 3 and abs((f.y + f.r) - t) < 3:
                        rect_match = True
                        break
                if rect_match:
                    # проверяем через WindowFromPoint, что там реально есть окно
                    if not HAS_WIN32:
                        supported = True
                    else:
                        hwnd = top_window_at(f.x, f.y + f.r + 1, my)
                        if hwnd and not self._is_bad_window(hwnd):
                            supported = True

            if supported:
                surviving.append(f)
            else:
                # отклеиваем — снова падает
                f.vy = 0.3
                f.gravity = 0.02
                self.flakes.append(f)

        self.landed = surviving

    # ---------- физика ----------
    def spawn(self):
        self.flakes.append(Flake(
            random.uniform(0, self.screen_w), -5,
            random.uniform(-0.4, 0.4), random.uniform(0.4, 1.2),
            random.uniform(1.2, 3.0), 0.01,
        ))

    def check_landing(self, x, y):
        """Есть ли реальное окно (не мы, не рабочий стол) в точке (x, y)?"""
        if not HAS_WIN32:
            return True
        hwnd = top_window_at(x, y, self.my_hwnd)
        if not hwnd:
            return False
        return not self._is_bad_window(hwnd)

    def tick(self):
        if random.random() < 0.9:
            self.spawn()

        surviving = []
        for f in self.flakes:
            if f.life is not None:
                f.life -= 1
                if f.life <= 0:
                    continue

            f.vx *= 0.995
            f.vy += f.gravity
            f.x += f.vx
            f.y += f.vy

            landed = False

            # пол / панель задач
            if f.y + f.r >= self.ground_y:
                f.y = self.ground_y - f.r
                landed = True

            # крыши окон
            if not landed:
                for (l, t, r, b) in self.window_rects:
                    if l - 3 <= f.x <= r + 3:
                        prev_bottom = f.y - f.vy + f.r
                        if prev_bottom <= t <= f.y + f.r + 0.5:
                            # реальная проверка: сверху ли это окно именно в этой точке
                            if self.check_landing(f.x, t + 1):
                                f.y = t - f.r
                                landed = True
                                break

            if landed:
                self.landed.append(f)
            else:
                surviving.append(f)

        self.flakes = [f for f in surviving
                       if -50 < f.x < self.screen_w + 50 and -50 < f.y < self.screen_h + 50]

        # Ограничение по памяти
        if len(self.landed) > 10000:
            self.landed = self.landed[-8000:]

        self.update()

    # ---------- рендер ----------
    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)

        p.setBrush(QColor(240, 245, 255, 230))
        for f in self.landed:
            p.drawEllipse(QPoint(int(f.x), int(f.y)), int(f.r), int(f.r))

        p.setBrush(QColor(255, 255, 255, 255))
        for f in self.flakes:
            p.drawEllipse(QPoint(int(f.x), int(f.y)), int(f.r), int(f.r))

        if self.play_mode:
            p.setPen(QColor(150, 200, 255, 100))
            p.setBrush(Qt.NoBrush)
            p.drawRect(1, 1, self.width() - 2, self.height() - 2)

        if self.grabbing and self.grab_charge > 0:
            radius = 25 + math.sqrt(self.grab_charge) * 3
            p.setPen(QColor(100, 150, 220, 150))
            p.setBrush(QColor(180, 210, 255, 70))
            p.drawEllipse(self.grab_pos, int(radius), int(radius))

    # ---------- режим игры ----------
    def set_play(self, on: bool):
        self.play_mode = on
        self.setAttribute(Qt.WA_TransparentForMouseEvents, not on)
        try:
            hwnd = int(self.winId())
            ex = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            if on:
                ex &= ~WS_EX_TRANSPARENT
            else:
                ex |= WS_EX_TRANSPARENT
            ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex)
        except Exception:
            pass
        self.update()

    def mousePressEvent(self, e):
        if not self.play_mode or e.button() != Qt.LeftButton:
            return
        self.grabbing = True
        self.grab_pos = e.pos()
        self.grab_charge = 0

    def mouseMoveEvent(self, e):
        if self.play_mode and self.grabbing:
            self.grab_pos = e.pos()
            self._absorb()

    def _absorb(self):
        radius = 25 + math.sqrt(self.grab_charge) * 3
        r2 = radius * radius
        px, py = self.grab_pos.x(), self.grab_pos.y()

        def close(f):
            dx, dy = f.x - px, f.y - py
            return dx * dx + dy * dy < r2

        before = len(self.landed) + len(self.flakes)
        self.landed = [f for f in self.landed if not close(f)]
        self.flakes = [f for f in self.flakes if not close(f)]
        self.grab_charge += before - len(self.landed) - len(self.flakes)

    def mouseReleaseEvent(self, e):
        if not (self.play_mode and e.button() == Qt.LeftButton and self.grabbing):
            return
        self.grabbing = False
        if self.grab_charge <= 0:
            return

        pos = e.pos()
        num = min(200, 5 + int(self.grab_charge / 1.5))
        for _ in range(num):
            angle = random.uniform(0, 2 * math.pi)
            speed = random.uniform(4, 14)
            vx = math.cos(angle) * speed
            vy = math.sin(angle) * speed - random.uniform(0, 3)
            self.flakes.append(Flake(
                pos.x(), pos.y(), vx, vy,
                random.uniform(1.5, 3.5), gravity=0.5, life=250,
            ))
        self.grab_charge = 0


def make_icon(color: QColor) -> QIcon:
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(color)
    p.setPen(Qt.NoPen)
    p.drawEllipse(8, 8, 48, 48)
    p.end()
    return QIcon(pm)


def main():
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    overlay = SnowOverlay()
    overlay.show()
    overlay.set_play(False)

    icon_normal = make_icon(QColor(180, 220, 255))
    icon_play = make_icon(QColor(255, 200, 100))

    tray = QSystemTrayIcon(icon_normal)
    tray.setToolTip("Снег на рабочем столе")

    menu = QMenu()
    toggle_action = QAction("Режим игры: ВЫКЛ")

    def toggle():
        overlay.set_play(not overlay.play_mode)
        toggle_action.setText("Режим игры: " + ("ВКЛ" if overlay.play_mode else "ВЫКЛ"))
        tray.setIcon(icon_play if overlay.play_mode else icon_normal)

    toggle_action.triggered.connect(toggle)
    menu.addAction(toggle_action)

    def clear():
        overlay.landed.clear()
        overlay.flakes.clear()
    clear_action = QAction("Очистить снег")
    clear_action.triggered.connect(clear)
    menu.addAction(clear_action)

    menu.addSeparator()
    quit_action = QAction("Выход")
    quit_action.triggered.connect(app.quit)
    menu.addAction(quit_action)

    tray.setContextMenu(menu)
    tray.activated.connect(lambda r: toggle() if r == QSystemTrayIcon.Trigger else None)
    tray.show()

    tray.showMessage(
        "Снег запущен",
        "Клик по иконке в трее — вкл/выкл режим игры.\n"
        "В режиме игры: зажми ЛКМ, води мышью чтобы собирать снег, отпусти — бросок.",
        QSystemTrayIcon.Information, 7000,
    )

    sys.exit(app.exec_())


if __name__ == "__main__":
    main()