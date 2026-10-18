# GNostr - Flatpak application build configuration
#
# This meson.build configures the project for GNOME-style Flatpak builds.
# Python source files are installed to pkgdatadir/gnostr so the launcher
# script can import them at runtime using sys.path manipulation.
project(
  'gnostr',
  [],
  version: '0.1.0',
  meson_version: '>= 1.4.0',
  default_options: [
    'buildtype=debugoptimized',
    'warning_level=2'
  ]
)
gnome = import('gnome')
i18n = import('i18n')
datadir = get_option('datadir')
pkgdatadir = get_option('prefix') / datadir
# Python installation for use by src/gnostr.in launcher
py_mod = import('python')
python_installation = py_mod.find_installation('python3')

# Sub-projects (order matters: po→data→src)
subdir('po')
subdir('data')
subdir('src')

# Run unit tests via pytest (per-file targets for Builder granularity)
pytest_prog = find_program('pytest', required: false)
if pytest_prog.found()
  test_env = {
    'PYTHONPATH': meson.project_source_root(),
  }
  test('test_gateway', pytest_prog, args: ['--verbose', meson.project_source_root() / 'tests/test_gateway.py'], env: test_env, timeout: 120)
  test('test_profile_service', pytest_prog, args: ['--verbose', meson.project_source_root() / 'tests/test_profile_service.py'], env: test_env, timeout: 120)
  test('test_profile_metadata', pytest_prog, args: ['--verbose', meson.project_source_root() / 'tests/test_profile_metadata_service.py'], env: test_env, timeout: 120)
  test('test_resource_management', pytest_prog, args: ['--verbose', meson.project_source_root() / 'tests/test_resource_management.py'], env: test_env, timeout: 120)
  test('test_following', pytest_prog, args: ['--verbose', meson.project_source_root() / 'tests/test_following.py'], env: test_env, timeout: 120)
  test('test_nip_builders', pytest_prog, args: ['--verbose', meson.project_source_root() / 'tests/test_nip_builders.py'], env: test_env, timeout: 120)
  test('test_publish_ack', pytest_prog, args: ['--verbose', meson.project_source_root() / 'tests/test_publish_ack.py'], env: test_env, timeout: 120)
  test('test_feed_pagination', pytest_prog, args: ['--verbose', meson.project_source_root() / 'tests/test_feed_pagination.py'], env: test_env, timeout: 120)
  test('test_feed_model', pytest_prog, args: ['--verbose', meson.project_source_root() / 'tests/test_feed_model.py'], env: test_env, timeout: 120)
  test('test_scroll_gate', pytest_prog, args: ['--verbose', meson.project_source_root() / 'tests/test_scroll_gate.py'], env: test_env, timeout: 120)
  test('test_scroll_gate_wiring', pytest_prog, args: ['--verbose', meson.project_source_root() / 'tests/test_scroll_gate_wiring.py'], env: test_env, timeout: 120)
  # VID-1: mobile video buffering — prefill props, threshold-gated play,
  # BUFFERING UX transitions + mid-playback pause-and-resume (mocked bus).
  test('test_video_buffering', pytest_prog, args: ['--verbose', meson.project_source_root() / 'tests/test_video_buffering.py'], env: test_env, timeout: 120)
  test('test_video_viewport_pause', pytest_prog, args: ['--verbose', meson.project_source_root() / 'tests/test_video_viewport_pause.py'], env: test_env, timeout: 120)
  test('test_video_perf_cap', pytest_prog, args: ['--verbose', meson.project_source_root() / 'tests/test_video_perf_cap.py'], env: test_env, timeout: 120)
  test('test_dm_relay_targeting', pytest_prog, args: ['--verbose', meson.project_source_root() / 'tests/test_dm_relay_targeting.py'], env: test_env, timeout: 120)
  test('test_dm_protocol_hardening', pytest_prog, args: ['--verbose', meson.project_source_root() / 'tests/test_dm_protocol_hardening.py'], env: test_env, timeout: 120)
  test('test_dm_recipient_view', pytest_prog, args: ['--verbose', meson.project_source_root() / 'tests/test_dm_recipient_view.py'], env: test_env, timeout: 120)
  test('test_dm_gtk422_api', pytest_prog, args: ['--verbose', meson.project_source_root() / 'tests/test_dm_gtk422_api.py'], env: test_env, timeout: 120)
  test('test_relay_log_wrap', pytest_prog, args: ['--verbose', meson.project_source_root() / 'tests/test_relay_log_wrap.py'], env: test_env, timeout: 120)
  test('test_clipboard_api', pytest_prog, args: ['--verbose', meson.project_source_root() / 'tests/test_clipboard_api.py'], env: test_env, timeout: 120)
  # Startup feed-freshness (spec Phase B): bounded freshness REQ window,
  # EOSE auto-close registration, pill routing during the hold, and the
  # FeedModel freshness-merge/echo-back dedup regression.
  test('test_feed_freshness', pytest_prog, args: ['--verbose', meson.project_source_root() / 'tests/test_feed_freshness.py'], env: test_env, timeout: 120)
  # Relay usage economy (issue #26): REQ token bucket, queue dedup, CLOSED
  # handling, reconnect backoff, live-sub replay, idle parking.
  test('test_relay_usage_economy', pytest_prog, args: ['--verbose', meson.project_source_root() / 'tests/test_relay_usage_economy.py'], env: test_env, timeout: 120)
  endif

# Real-GTK feed harness (tier 2): exercises ui.feed_view.FeedView against a
# real Gtk.ScrolledWindow + real FeedModel + real Database. Needs a display
# (xvfb-run) and real GTK4/Adw (the Flatpak SDK python3 in Builder). The
# script self-skips with exit code 77 (meson "skip") when either is
# unavailable, so it is safe to register unconditionally; only registered
# when both programs exist so a build without them stays quiet.
xvfb_run = find_program('xvfb-run', required: false)
gtk_python = find_program('python3', required: false)
if xvfb_run.found() and gtk_python.found()
  test('test_feed_gtk_real', xvfb_run,
    args: ['-a', gtk_python, meson.project_source_root() / 'tests/test_feed_gtk_real.py'],
    timeout: 120)
  # Real-GTK clipboard harness (issue #33): ExportKeyDialog.on_copy ->
  # real Gdk clipboard -> read_text_async read-back.
  test('test_clipboard_gtk_real', xvfb_run,
    args: ['-a', gtk_python, meson.project_source_root() / 'tests/test_clipboard_gtk_real.py'],
    timeout: 120)
  # Real-GTK recipient-picker smoke (Phase A2): construct + filter +
  # paste-pin + activate against real Gtk/Adw.
  test('test_dm_recipient_gtk_real', xvfb_run,
    args: ['-a', gtk_python, meson.project_source_root() / 'tests/test_dm_recipient_gtk_real.py'],
    timeout: 120)
endif
