import sys
import os
import threading
import time
import random
from functools import partial

from PyQt5 import QtWidgets, QtCore, QtGui

import win32gui
import win32con
import win32api

from pynput import keyboard
from pynput.mouse import Controller as MouseController, Button
try:
	from PyQt5.QtWinExtras import QtWin
	_HAS_QTWIN = True
except Exception:
	_HAS_QTWIN = False


def make_lparam(x, y):
	return (y << 16) | (x & 0xffff)



class HotkeyCaptureDialog(QtWidgets.QDialog):
	"""Dialog that captures a key combination using Qt key events.
	Emits `hotkey_applied(str)` where str is in pynput GlobalHotKeys format,
	e.g. '<ctrl>+<alt>+a'. Provides Clear and Apply buttons.
	"""
	hotkey_applied = QtCore.pyqtSignal(str)

	def __init__(self, parent=None, initial=''):
		super().__init__(parent)
		self.setWindowTitle('Capture Hotkey')
		self.setModal(True)
		self.setMinimumWidth(360)

		self.captured = initial or ''
		self._mods = set()
		self._keys = []

		layout = QtWidgets.QVBoxLayout(self)
		layout.addWidget(QtWidgets.QLabel('Press the desired key combination now (modifiers + key).'))

		self.display = QtWidgets.QLabel(self.captured or 'No hotkey recorded')
		self.display.setStyleSheet('background: #161719; border: 1px solid #2a2b2f; padding: 8px; border-radius:6px;')
		layout.addWidget(self.display)

		btn_row = QtWidgets.QHBoxLayout()
		self.clear_btn = QtWidgets.QPushButton('Clear')
		self.apply_btn = QtWidgets.QPushButton('Apply')
		self.cancel_btn = QtWidgets.QPushButton('Cancel')
		btn_row.addWidget(self.clear_btn)
		btn_row.addWidget(self.apply_btn)
		btn_row.addWidget(self.cancel_btn)
		layout.addLayout(btn_row)

		self.clear_btn.clicked.connect(self.on_clear)
		self.apply_btn.clicked.connect(self.on_apply)
		self.cancel_btn.clicked.connect(self.reject)

		self.setFocusPolicy(QtCore.Qt.StrongFocus)
		self.setFocus()

	def on_clear(self):
		self.captured = ''
		self._mods.clear()
		self._keys.clear()
		self.display.setText('No hotkey recorded')

	def on_apply(self):
		if not self.captured:
			QtWidgets.QMessageBox.warning(self, 'No hotkey', 'No hotkey recorded.')
			return
		self.hotkey_applied.emit(self.captured)
		self.accept()

	def keyPressEvent(self, event):
		m = event.modifiers()
		# track modifiers
		self._mods.clear()
		if m & QtCore.Qt.ControlModifier:
			self._mods.add('ctrl')
		if m & QtCore.Qt.AltModifier:
			self._mods.add('alt')
		if m & QtCore.Qt.ShiftModifier:
			self._mods.add('shift')
		if m & QtCore.Qt.MetaModifier:
			self._mods.add('cmd')

		# capture one or more base keys (so user can press A then B to make a+b)
		key = event.key()
		name = self.qt_key_to_name(key, event)
		if name:
			if name not in self._keys:
				self._keys.append(name)
			# build hotkey string in pynput format: modifiers then keys separated by +
			parts = [f'<{m}>' for m in sorted(self._mods)]
			parts += self._keys
			self.captured = '+'.join(parts) if parts else ''
			self.display.setText(self.captured)

	def qt_key_to_name(self, key, event):
		# Letters and digits via keycode (avoids relying on event.text which
		# can be empty or produce non-printable characters when modifiers are held).
		if QtCore.Qt.Key_A <= key <= QtCore.Qt.Key_Z:
			return chr(ord('a') + (key - QtCore.Qt.Key_A))
		if QtCore.Qt.Key_0 <= key <= QtCore.Qt.Key_9:
			return chr(ord('0') + (key - QtCore.Qt.Key_0))
		# fallback to printable text
		text = event.text()
		if text and len(text.strip()) == 1:
			return text.lower()
		# Function keys
		if QtCore.Qt.Key_F1 <= key <= QtCore.Qt.Key_F35:
			num = key - QtCore.Qt.Key_F1 + 1
			return f'f{num}'
		mapping = {
			QtCore.Qt.Key_Enter: 'enter',
			QtCore.Qt.Key_Return: 'enter',
			QtCore.Qt.Key_Space: 'space',
			QtCore.Qt.Key_Escape: 'esc',
			QtCore.Qt.Key_Tab: 'tab',
			QtCore.Qt.Key_Backspace: 'backspace',
			QtCore.Qt.Key_Insert: 'insert',
			QtCore.Qt.Key_Delete: 'delete',
			QtCore.Qt.Key_Home: 'home',
			QtCore.Qt.Key_End: 'end',
			QtCore.Qt.Key_PageUp: 'pageup',
			QtCore.Qt.Key_PageDown: 'pagedown',
			QtCore.Qt.Key_Left: 'left',
			QtCore.Qt.Key_Right: 'right',
			QtCore.Qt.Key_Up: 'up',
			QtCore.Qt.Key_Down: 'down',
		}
		return mapping.get(key, '')


class AutoClicker(QtWidgets.QMainWindow):
	def __init__(self):
		super().__init__()
		self.setWindowTitle('AutoClicker — Modern Dark')
		self.setMinimumSize(800, 450)

		# Set window icon
		icon_path = 'autoclicker.ico'
		if os.path.exists(icon_path):
			self.setWindowIcon(QtGui.QIcon(icon_path))

		# Resize tracking: if the user manually resizes the window, keep that size.
		# Programmatic resizes set `_programmatic_resize` to avoid marking them
		# as user actions.
		self._user_resized = False
		self._programmatic_resize = False

		self.running = False
		self.click_thread = None
		self.hotkey_listener = None
		self.hotkey = '<ctrl>+<alt>+a'

		self.target_hwnd = None
		self.target_point = None  # tuple (x,y)
		self.target_point_type = None  # 'client' or 'screen'

		self.init_ui()
		self.set_dark_style()

	def init_ui(self):
		w = QtWidgets.QWidget()
		self.setCentralWidget(w)
		layout = QtWidgets.QVBoxLayout(w)
		layout.setSpacing(10)
		layout.setContentsMargins(12,12,12,12)

		# Mode selection
		mode_layout = QtWidgets.QHBoxLayout()
		self.mode_combo = QtWidgets.QComboBox()
		self.mode_combo.addItems(['Fixed CPS', 'Random CPS Range'])
		mode_layout.addWidget(QtWidgets.QLabel('Mode:'))
		mode_layout.addWidget(self.mode_combo)
		layout.addLayout(mode_layout)

		# Fixed CPS
		# Fixed CPS (wrapped so we can hide/show it)
		self.fixed_widget = QtWidgets.QWidget()
		fixed_layout = QtWidgets.QHBoxLayout(self.fixed_widget)
		self.fixed_spin = QtWidgets.QDoubleSpinBox()
		self.fixed_spin.setRange(0.1, 1000)
		self.fixed_spin.setValue(10.0)
		self.fixed_spin.setSingleStep(0.1)
		fixed_layout.addWidget(QtWidgets.QLabel('Fixed CPS:'))
		fixed_layout.addWidget(self.fixed_spin)
		layout.addWidget(self.fixed_widget)

		# Range (wrapped widget so we can hide/show it)
		self.range_widget = QtWidgets.QWidget()
		range_layout = QtWidgets.QHBoxLayout(self.range_widget)
		self.min_spin = QtWidgets.QDoubleSpinBox()
		self.max_spin = QtWidgets.QDoubleSpinBox()
		for sp in (self.min_spin, self.max_spin):
			sp.setRange(0.1, 1000)
			sp.setSingleStep(0.1)
		self.min_spin.setValue(7.0)
		self.max_spin.setValue(12.0)
		range_layout.addWidget(QtWidgets.QLabel('Min CPS:'))
		range_layout.addWidget(self.min_spin)
		range_layout.addWidget(QtWidgets.QLabel('Max CPS:'))
		range_layout.addWidget(self.max_spin)
		layout.addWidget(self.range_widget)

		# Target window selection + point picking (split so point is always available)
		target_group = QtWidgets.QGroupBox('Target Window / Click Location')
		tg_layout = QtWidgets.QVBoxLayout()

		# Window selector area (only shown for PostMessage method)
		self.win_select_widget = QtWidgets.QWidget()
		win_sel_layout = QtWidgets.QHBoxLayout(self.win_select_widget)
		self.win_combo = QtWidgets.QComboBox()
		self.refresh_btn = QtWidgets.QPushButton('Refresh Windows')
		self.refresh_btn.clicked.connect(self.refresh_windows)
		win_sel_layout.addWidget(self.win_combo)
		win_sel_layout.addWidget(self.refresh_btn)
		tg_layout.addWidget(self.win_select_widget)

		# Point picking area (always visible)
		self.point_widget = QtWidgets.QWidget()
		point_layout = QtWidgets.QHBoxLayout(self.point_widget)
		self.pick_point_btn = QtWidgets.QPushButton('Pick Point (hover then press Enter)')
		self.pick_point_btn.clicked.connect(self.pick_point)
		self.clear_point_btn = QtWidgets.QPushButton('Clear Point')
		self.clear_point_btn.clicked.connect(self.clear_point)
		point_layout.addWidget(self.pick_point_btn)
		point_layout.addWidget(self.clear_point_btn)
		tg_layout.addWidget(self.point_widget)

		self.point_label = QtWidgets.QLabel('No point selected')
		tg_layout.addWidget(self.point_label)

		target_group.setLayout(tg_layout)
		self.target_group = target_group
		layout.addWidget(self.target_group)

		# Hotkey display + change button
		hk_layout = QtWidgets.QHBoxLayout()
		self.hotkey_label = QtWidgets.QLabel(self.hotkey)
		self.change_hotkey_btn = QtWidgets.QPushButton('Change Hotkey')
		self.change_hotkey_btn.clicked.connect(self.open_hotkey_dialog)
		hk_layout.addWidget(QtWidgets.QLabel('Toggle Hotkey:'))
		hk_layout.addWidget(self.hotkey_label)
		hk_layout.addWidget(self.change_hotkey_btn)
		layout.addLayout(hk_layout)

		# Start/Stop
		ss_layout = QtWidgets.QHBoxLayout()
		self.toggle_btn = QtWidgets.QPushButton('Start')
		self.toggle_btn.setShortcut('F6')
		self.toggle_btn.clicked.connect(self.toggle_running)
		ss_layout.addStretch()
		ss_layout.addWidget(self.toggle_btn)
		layout.addLayout(ss_layout)

		# Advanced: normal click method
		adv_layout = QtWidgets.QHBoxLayout()
		self.method_combo = QtWidgets.QComboBox()
		self.method_combo.addItems(['PostMessage to target window (no focus)', 'Global mouse clicks (requires focus)'])
		adv_layout.addWidget(QtWidgets.QLabel('Method:'))
		adv_layout.addWidget(self.method_combo)
		layout.addLayout(adv_layout)

		# Wire visibility changes
		self.mode_combo.currentIndexChanged.connect(self.update_visibility)
		self.method_combo.currentIndexChanged.connect(self.update_visibility)

		self.update_visibility()

		self.status = QtWidgets.QLabel('Ready')
		layout.addWidget(self.status)

		self.refresh_windows()
		self.start_hotkey_listener()

	def set_dark_style(self):
		dark = """
		QWidget { background: #0f1113; color: #e6e6e6; font-family: 'Segoe UI'; font-size: 10pt; }
		QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QPushButton { background: #161719; border: 1px solid #2a2b2f; padding: 6px; border-radius:6px; }
		QPushButton { padding-top:8px; padding-bottom:8px; }
		QPushButton:hover { background: #232426; }
		QGroupBox { border: 1px solid #2a2b2f; margin-top: 8px; border-radius:6px; }
		QGroupBox::title { subcontrol-origin: margin; left: 8px; padding: 0 6px 0 6px; }
		QLabel { padding: 2px; }
		"""
		self.setStyleSheet(dark)
		# Improve font slightly
		self.setFont(QtGui.QFont('Segoe UI', 10))

	def update_visibility(self):
		# Show range only when Random CPS Range is selected (index 1)
		if self.mode_combo.currentIndex() == 1:
			self.range_widget.show()
			self.fixed_widget.hide()
		else:
			self.range_widget.hide()
			self.fixed_widget.show()

		# Keep the target group visible so the point picker is always accessible.
		# Only hide the window-selection area when not using PostMessage.
		self.target_group.show()
		if self.method_combo.currentIndex() == 0:
			self.target_group.setTitle('Target Window / Click Location')
			self.win_select_widget.show()
		else:
			self.target_group.setTitle('Click Location')
			self.win_select_widget.hide()

		# Update minimum size to match visible content
		self.update_minimum_size()

	def update_minimum_size(self):
		# calculate size hint from central widget and set a reasonable minimum
		try:
			cw = self.centralWidget()
			if cw is None:
				return
			hint = cw.sizeHint()
			minw = max(480, hint.width() + 40)
			minh = max(300, hint.height() + 40)
			# Only increase minimum size, don't force-shrink user's larger window
			self.setMinimumSize(minw, minh)
			# If the user hasn't manually resized, enforce the dynamic size so
			# the window always fits visible widgets. Use a programmatic flag
			# to avoid tagging this resize as user-initiated.
			if not getattr(self, '_user_resized', False):
				try:
					self._programmatic_resize = True
					self.resize(minw, minh)
				finally:
					self._programmatic_resize = False
		except Exception:
			pass

	def showEvent(self, event):
		super().showEvent(event)
		# Do the initial resize shortly after showing so layouts are settled
		if not getattr(self, '_did_initial_resize', False):
			QtCore.QTimer.singleShot(40, self._do_initial_resize)
			self._did_initial_resize = True

	def _do_initial_resize(self):
		try:
			# ensure visibility-driven layout is up to date
			self.update_visibility()
			self.update_minimum_size()
			minw = self.minimumSize().width()
			minh = self.minimumSize().height()
			cur = self.size()
			neww = max(cur.width(), minw)
			newh = max(cur.height(), minh)
			# Programmatic resize so we don't mark this as a user resize
			try:
				self._programmatic_resize = True
				self.resize(neww, newh)
			finally:
				self._programmatic_resize = False
		except Exception:
			pass

	def resizeEvent(self, event):
		# If a resize wasn't initiated programmatically, treat it as a user resize
		if not getattr(self, '_programmatic_resize', False):
			self._user_resized = True
		super().resizeEvent(event)

	def refresh_windows(self):
		self.win_combo.clear()
		def enum(hwnd, results):
			if win32gui.IsWindowVisible(hwnd) and win32gui.GetWindowText(hwnd):
				results.append((hwnd, win32gui.GetWindowText(hwnd)))
		results = []
		win32gui.EnumWindows(enum, results)
		for hwnd, title in results:
			display = f"{title} ({hwnd})"
			icon = None
			if _HAS_QTWIN:
				try:
					# Try various WM_GETICON sizes
					hicon = win32gui.SendMessage(hwnd, win32con.WM_GETICON, 1, 0)
					if not hicon:
						hicon = win32gui.SendMessage(hwnd, win32con.WM_GETICON, 0, 0)
					if not hicon:
						# try class icon
						try:
							hicon = win32gui.GetClassLong(hwnd, win32con.GCL_HICON)
						except Exception:
							hicon = None
					if hicon:
						qicon = QtGui.QIcon(QtWin.fromHICON(int(hicon)))
						icon = qicon
				except Exception:
					icon = None
			if icon:
				self.win_combo.addItem(icon, display, hwnd)
			else:
				self.win_combo.addItem(display, hwnd)

	def pick_point(self):
		# Behavior depends on selected method
		method = self.method_combo.currentIndex()
		if method == 0:
			# PostMessage: require a selected window
			idx = self.win_combo.currentIndex()
			if idx < 0:
				QtWidgets.QMessageBox.warning(self, 'Select Window', 'Please select a target window first.')
				return
			hwnd = self.win_combo.currentData()
			self.target_hwnd = hwnd
			QtWidgets.QMessageBox.information(self, 'Pick Point', 'Move your mouse to the desired point inside the target window, then press ENTER. (You may need to focus the target window briefly to place the cursor)')
			# Listen for Enter once and record current cursor pos, convert to client coords
			def on_press(key):
				try:
					if key == keyboard.Key.enter:
						x, y = win32api.GetCursorPos()
						client = win32gui.ScreenToClient(hwnd, (x, y))
						self.target_point = client
						self.target_point_type = 'client'
						self.point_label.setText(f'Point (client): {client[0]}, {client[1]} (hwnd {hwnd})')
						return False
				except Exception:
					return False

			with keyboard.Listener(on_press=on_press) as listener:
				listener.join()
		else:
			# Global click: capture screen coords
			QtWidgets.QMessageBox.information(self, 'Pick Point', 'Move your mouse to the desired screen point, then press ENTER.')
			def on_press2(key):
				try:
					if key == keyboard.Key.enter:
						x, y = win32api.GetCursorPos()
						self.target_point = (x, y)
						self.target_point_type = 'screen'
						self.target_hwnd = None
						self.point_label.setText(f'Point (screen): {x}, {y}')
						return False
				except Exception:
					return False

			with keyboard.Listener(on_press=on_press2) as listener:
				listener.join()

	def clear_point(self):
		self.target_point = None
		self.target_point_type = None
		self.target_hwnd = None
		self.point_label.setText('No point selected')

	def start_hotkey_listener(self):
		self.stop_hotkey_listener()
		# Build mapping
		try:
			# hotkey stored in self.hotkey
			# Use a thread-safe trigger that invokes the toggle on the Qt main thread
			def _trigger():
				QtCore.QMetaObject.invokeMethod(self, 'toggle_running', QtCore.Qt.QueuedConnection)
			mapping = {self.hotkey: _trigger}
			self.hotkey_listener = keyboard.GlobalHotKeys(mapping)
			t = threading.Thread(target=self.hotkey_listener.start, daemon=True)
			t.start()
			self.status.setText(f'Hotkey active: {self.hotkey}')
		except Exception as e:
			self.status.setText(f'Hotkey error: {e}')

	def stop_hotkey_listener(self):
		if self.hotkey_listener:
			try:
				self.hotkey_listener.stop()
			except Exception:
				pass
			self.hotkey_listener = None

	def set_hotkey(self):
		# Deprecated UI path — use open_hotkey_dialog
		self.stop_hotkey_listener()
		self.start_hotkey_listener()

	def open_hotkey_dialog(self):
		# Temporarily stop the global hotkey listener so capturing combos
		# (including the current binding) doesn't trigger the app.
		self.stop_hotkey_listener()
		prev_status = self.status.text()
		self.status.setText('Capturing hotkey...')
		d = HotkeyCaptureDialog(self, initial=self.hotkey)
		d.hotkey_applied.connect(self.apply_new_hotkey)
		res = d.exec_()
		# If dialog was cancelled (no new hotkey applied), restart listener
		if res == QtWidgets.QDialog.Rejected:
			self.start_hotkey_listener()
			self.status.setText(prev_status)

	def apply_new_hotkey(self, hk_str):
		# Update label and restart listener
		self.hotkey = hk_str
		self.hotkey_label.setText(self.hotkey)
		self.stop_hotkey_listener()
		self.start_hotkey_listener()

	@QtCore.pyqtSlot()
	def toggle_running(self):
		if self.running:
			self.stop_clicking()
		else:
			self.start_clicking()

	def start_clicking(self):
		self.running = True
		self.toggle_btn.setText('Stop')
		self.status.setText('Running')
		self.click_thread = threading.Thread(target=self.click_loop, daemon=True)
		self.click_thread.start()

	def stop_clicking(self):
		self.running = False
		self.toggle_btn.setText('Start')
		self.status.setText('Stopped')
		# thread will exit

	def click_once_to_hwnd(self, hwnd, x, y):
		lparam = make_lparam(x, y)
		try:
			win32gui.PostMessage(hwnd, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, lparam)
			time.sleep(0.01)
			win32gui.PostMessage(hwnd, win32con.WM_LBUTTONUP, None, lparam)
		except Exception as e:
			print('PostMessage error', e)

	def click_once_global(self, x, y):
		mouse = MouseController()
		mouse.position = (x, y)
		mouse.click(Button.left, 1)

	def click_loop(self):
		method = self.method_combo.currentIndex()
		while self.running:
			try:
				if self.mode_combo.currentIndex() == 0:
					cps = float(self.fixed_spin.value())
				else:
					mn = float(self.min_spin.value())
					mx = float(self.max_spin.value())
					if mn > mx:
						mn, mx = mx, mn
					cps = random.uniform(mn, mx)
				delay = max(0.0001, 1.0 / cps)

				if self.target_point:
					x, y = self.target_point
					if method == 0 and self.target_point_type == 'client' and self.target_hwnd:
						# PostMessage to window (client coords)
						self.click_once_to_hwnd(self.target_hwnd, int(x), int(y))
					elif method == 1 and self.target_point_type == 'screen':
						# global click at specific screen coordinates
						self.click_once_global(int(x), int(y))
					elif method == 1 and self.target_point_type == 'client' and self.target_hwnd:
						# convert client to screen then global click
						screen_pt = win32gui.ClientToScreen(self.target_hwnd, (int(x), int(y)))
						self.click_once_global(screen_pt[0], screen_pt[1])
					else:
						# Fallback: use current cursor position
						pos = win32api.GetCursorPos()
						self.click_once_global(pos[0], pos[1])
				else:
					# No specific target point — click at current cursor
					pos = win32api.GetCursorPos()
					self.click_once_global(pos[0], pos[1])

				# small human-like jitter between clicks
				time.sleep(delay * (0.9 + random.random() * 0.2))
			except Exception as e:
				print('Click loop error', e)
				time.sleep(0.1)


def main():
	app = QtWidgets.QApplication(sys.argv)
	win = AutoClicker()
	win.show()
	sys.exit(app.exec_())


if __name__ == '__main__':
	main()

