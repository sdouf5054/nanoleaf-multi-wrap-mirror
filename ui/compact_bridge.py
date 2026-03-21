"""CompactBridge v3 — CompactWindow ↔ ControlTab 양방향 동기화 중재자

[v3 변경]
- 미디어 소스 문구/색상: DisplayMirrorSection.update_current_source()와
  완전 동일한 로직 + palette 색상 사용
- sync_media_info에 source_color 파라미터 추가
- 프리셋 되돌리기 시 밝기 동기화 누락 수정
- 새로고침 버튼 시그널 연결 재확인
- on_compact_shown에서 즉시 미디어+리소스 갱신
"""

import os
from PySide6.QtCore import QObject, QTimer, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QColorDialog

from core.preset import list_presets
from core.media_session import HAS_MEDIA_SESSION
from styles.palette import current as _pal_current

try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False


class CompactBridge(QObject):
    """CompactWindow ↔ ControlTab 양방향 동기화."""

    def __init__(self, compact, tab, engine_ctrl, config, parent=None):
        super().__init__(parent)
        self._compact = compact
        self._tab = tab
        self._engine_ctrl = engine_ctrl
        self._config = config

        self._process = None
        if HAS_PSUTIL:
            self._process = psutil.Process(os.getpid())
            self._process.cpu_percent()

        self._resource_timer = QTimer(self)
        self._resource_timer.setInterval(2000)
        self._resource_timer.timeout.connect(self._update_resources)

        self._media_timer = QTimer(self)
        self._media_timer.setInterval(3000)
        self._media_timer.timeout.connect(self._update_media_card)

    # ══════════════════════════════════════════════════════════════
    #  연결
    # ══════════════════════════════════════════════════════════════

    def connect_all(self):
        self._connect_compact_to_tab()
        self._connect_engine_to_compact()
        self._initial_sync()

    def _connect_compact_to_tab(self):
        c = self._compact
        c.request_start.connect(self._on_compact_start)
        c.request_stop.connect(self._on_compact_stop)
        c.toggle_display_changed.connect(self._on_compact_display)
        c.toggle_audio_changed.connect(self._on_compact_audio)
        c.toggle_media_changed.connect(self._on_compact_media)
        c.master_brightness_changed.connect(self._on_compact_brightness)
        c.audio_mode_changed.connect(self._on_compact_audio_mode)
        c.preset_selected.connect(self._on_compact_preset)
        c.preset_set_default.connect(self._on_compact_preset_default)
        c.color_preset_changed.connect(self._on_compact_color)
        c.color_effect_changed.connect(self._on_compact_effect)
        c.effect_speed_changed.connect(self._on_compact_speed)
        c.media_fix_changed.connect(self._on_compact_media_fix)
        c.media_source_swap.connect(self._on_compact_media_swap)
        c.media_refresh.connect(self._on_compact_media_refresh)
        c.close_requested.connect(self._on_compact_close)

    def _connect_engine_to_compact(self):
        ctrl = self._engine_ctrl
        ctrl.fps_updated.connect(self._compact.sync_fps)
        ctrl.status_changed.connect(self._compact.sync_status)
        ctrl.running_changed.connect(self._on_running_changed)
        ctrl.energy_updated.connect(self._compact.sync_energy)

    def _initial_sync(self):
        t = self._tab
        c = self._compact
        c.sync_state(
            display_on=t._display_on,
            audio_on=t._audio_on,
            media_on=t._media_on,
            is_running=t._is_running,
            brightness=t.slider_master_brightness.value(),
            audio_mode=t.section_audio._mode_key,
        )
        self._sync_preset_list()
        self._sync_color_state()
        c.toggle_media.setEnabled(t._display_on and HAS_MEDIA_SESSION)

    # ══════════════════════════════════════════════════════════════
    #  CompactWindow → ControlTab
    # ══════════════════════════════════════════════════════════════

    def _on_compact_start(self):
        self._tab._on_start_clicked()

    def _on_compact_stop(self):
        self._tab.request_engine_stop.emit()

    def _on_compact_display(self, checked):
        if self._tab._display_on != checked:
            self._tab.toggle_display.setChecked(checked)

    def _on_compact_audio(self, checked):
        if self._tab._audio_on != checked:
            self._tab.toggle_audio.setChecked(checked)

    def _on_compact_media(self, checked):
        if self._tab._media_on != checked:
            self._tab.toggle_media.setChecked(checked)

    def _on_compact_brightness(self, value):
        self._tab.slider_master_brightness.setValue(value)

    def _on_compact_audio_mode(self, mode_key):
        from ui.panels.audio_reactive_section import _MODE_TO_INDEX
        section = self._tab.section_audio
        idx = _MODE_TO_INDEX.get(mode_key, 0)
        section._save_mode_params(section._mode_key)
        section._mode_key = mode_key
        section._load_mode_params(mode_key)
        section.combo_audio_mode.blockSignals(True)
        section.combo_audio_mode.setCurrentIndex(idx)
        section.combo_audio_mode.blockSignals(False)
        section._update_mode_visibility()
        self._tab._sync_flowing_state()
        self._tab._check_preset_modified()
        if self._tab._is_running:
            self._tab._push_params_to_engine()

    def _on_compact_preset(self, name):
        self._tab.select_preset_by_name(name)
        if not self._engine_ctrl.is_running:
            self._tab._on_start_clicked()
        self._full_sync_to_compact()

    def _on_compact_preset_default(self, name):
        opts = self._config.setdefault("options", {})
        old_default = opts.get("default_preset")
        if old_default == name:
            opts["default_preset"] = None
        else:
            opts["default_preset"] = name
        self._tab.config_applied.emit()
        self._sync_preset_list()

    def _on_compact_color(self, name, rgb):
        section = self._tab.section_color
        if name == "custom":
            r, g, b = section._current_color
            color = QColorDialog.getColor(QColor(r, g, b), self._compact, "기본 색상")
            if color.isValid():
                section._set_color(color.red(), color.green(), color.blue())
                self._sync_color_state()
            return
        if rgb is None:
            section._set_rainbow()
        else:
            section._set_color(*rgb)

    def _on_compact_effect(self, effect_key):
        section = self._tab.section_color
        from ui.panels.display_color_section import _COLOR_EFFECT_TO_INDEX
        idx = _COLOR_EFFECT_TO_INDEX.get(effect_key, 0)
        section.combo_color_effect.setCurrentIndex(idx)

    def _on_compact_speed(self, value):
        self._tab.section_color.slider_gradient_speed.setValue(value)

    def _on_compact_media_fix(self, checked):
        section = self._tab.section_mirror
        if checked:
            decision = self._get_current_media_decision()
            override = decision
        else:
            override = "auto"
        for i in range(section.combo_media_source.count()):
            if section.combo_media_source.itemData(i) == override:
                section.combo_media_source.setCurrentIndex(i)
                break

    def _on_compact_media_swap(self):
        """소스 전환 — 큰 GUI의 '⇄ 전환' 버튼 클릭과 동일.
        DisplayMirrorSection.btn_toggle_source.click()을 직접 호출.
        """
        section = self._tab.section_mirror
        if hasattr(section, 'btn_toggle_source'):
            section.btn_toggle_source.click()

    def _on_compact_media_refresh(self):
        """★ 컴팩트 새로고침 → ControlTab의 기존 로직 호출."""
        self._tab._on_refresh_thumbnail_requested()
        # 즉시 미디어 카드도 갱신
        QTimer.singleShot(500, self._update_media_card)

    def _on_compact_close(self):
        self._stop_timers()

    # ══════════════════════════════════════════════════════════════
    #  ControlTab → CompactWindow
    # ══════════════════════════════════════════════════════════════

    def _on_running_changed(self, running):
        self._compact.sync_running_state(running)
        if running:
            self._start_timers()
        else:
            self._stop_media_timer()

    def _full_sync_to_compact(self):
        t = self._tab
        self._compact.sync_state(
            display_on=t._display_on,
            audio_on=t._audio_on,
            media_on=t._media_on,
            is_running=t._is_running,
            brightness=t.slider_master_brightness.value(),
            audio_mode=t.section_audio._mode_key,
        )
        self._sync_preset_list()
        self._sync_color_state()

    def notify_tab_changed(self):
        if self._compact.isVisible():
            self._full_sync_to_compact()

    def notify_preset_changed(self):
        if self._compact.isVisible():
            self._sync_preset_list()

    # ══════════════════════════════════════════════════════════════
    #  주기적 갱신
    # ══════════════════════════════════════════════════════════════

    def _start_timers(self):
        if not self._resource_timer.isActive():
            self._resource_timer.start()
        if self._tab._media_on and not self._media_timer.isActive():
            self._media_timer.start()

    def _stop_timers(self):
        self._resource_timer.stop()
        self._media_timer.stop()

    def _stop_media_timer(self):
        self._media_timer.stop()

    def _update_resources(self):
        if not self._compact.isVisible() or self._process is None:
            return
        try:
            cpu = self._process.cpu_percent() / (psutil.cpu_count() or 1)
            mem_info = self._process.memory_full_info()
            ram = getattr(mem_info, 'uss', mem_info.rss) / (1024 * 1024)
            self._compact.sync_resource(cpu, ram)
        except Exception:
            pass

    def _update_media_card(self):
        """★ 미디어 문구/색상을 기존 DisplayMirrorSection.update_current_source()와
        완전 동일한 로직으로 구성."""
        if not self._compact.isVisible():
            return
        if not self._tab._media_on or not self._tab._is_running:
            return
        if not self._engine_ctrl or not self._engine_ctrl.engine:
            return

        engine = self._engine_ctrl.engine
        provider = getattr(engine, '_media_provider', None)
        if provider is None:
            return

        pal = _pal_current()

        # ── 소스 상태 + 색상 (기존 DisplayMirrorSection과 동일) ──
        decision = getattr(engine, '_media_detect_decision', "media")
        state = getattr(engine, '_media_detect_state', "idle")
        override = self._tab.section_mirror.combo_media_source.currentData()

        if override == "auto":
            if decision == "media":
                if state == "phase1":
                    source_text = "미디어 (판별 중...)"
                    source_color = pal["media_phase1"]
                else:
                    source_text = "미디어 사용 중"
                    source_color = pal["media_active"]
            else:
                if state == "audio_idle":
                    source_text = "미러링 (오디오 무음)"
                    source_color = pal["media_idle"]
                elif state == "phase1":
                    source_text = "미러링 (판별 중...)"
                    source_color = pal["media_phase1"]
                else:
                    source_text = "미러링 사용 중"
                    source_color = pal["media_mirror"]
        elif override == "media":
            source_text = "미디어 사용 중 (고정)"
            source_color = pal["media_active"]
        else:
            source_text = "미러링 사용 중 (고정)"
            source_color = pal["media_mirror"]

        # ── 곡 정보 (기존 tab_control._update_media_thumbnail과 동일) ──
        info = provider.get_media_info()
        if info:
            artist = info.get("artist", "")
            title = info.get("title", "")
            if artist and title:
                song_text = f"♪ {artist} — {title}"
            elif title:
                song_text = f"♪ {title}"
            else:
                song_text = "재생 중인 미디어 없음"
        else:
            song_text = "재생 중인 미디어 없음"
            source_text = "미디어 연동 활성"
            source_color = pal["media_active"]

        # ── 썸네일 ──
        thumb_pixmap = None
        frame = provider.get_frame()
        if frame is not None:
            try:
                from PySide6.QtGui import QImage, QPixmap
                h, w = frame.shape[:2]
                qimg = QImage(frame.data, w, h, 3 * w, QImage.Format.Format_RGB888)
                pixmap = QPixmap.fromImage(qimg)
                thumb_pixmap = pixmap.scaled(
                    42, 42,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            except Exception:
                pass

        self._compact.sync_media_info(source_text, source_color, song_text, thumb_pixmap)

        # fix 체크박스 동기화
        current_override = self._tab.section_mirror.combo_media_source.currentData()
        is_fixed = current_override in ("media", "mirror")
        self._compact.chk_media_fix.blockSignals(True)
        self._compact.chk_media_fix.setChecked(is_fixed)
        self._compact.chk_media_fix.blockSignals(False)

    # ══════════════════════════════════════════════════════════════
    #  헬퍼
    # ══════════════════════════════════════════════════════════════

    def _sync_preset_list(self):
        names = list_presets()
        current = self._tab.current_preset_name
        default_name = self._config.get("options", {}).get("default_preset")
        self._compact.sync_preset_list(names, current, default_name)

    def _sync_color_state(self):
        section = self._tab.section_color
        self._compact.sync_color_state(
            rainbow=section._is_rainbow,
            base_color=section._current_color,
            effect=section._color_effect,
            speed=section.slider_gradient_speed.value(),
        )

    def _get_current_media_decision(self):
        if self._engine_ctrl and self._engine_ctrl.engine:
            return getattr(self._engine_ctrl.engine, '_media_detect_decision', "media")
        return "media"

    def on_compact_shown(self):
        self._full_sync_to_compact()
        if self._tab._is_running:
            self._start_timers()
            self._update_media_card()
            self._update_resources()

    def on_compact_hidden(self):
        self._stop_timers()

    def cleanup(self):
        self._stop_timers()