application_id = 'com.damiandudycz.CatalystLab'

scalable_dir = 'hicolor' / 'scalable' / 'apps'
install_data(
  scalable_dir / ('@0@.svg').format(application_id),
  install_dir: get_option('datadir') / 'icons' / scalable_dir
)

symbolic_dir = 'hicolor' / 'symbolic' / 'apps'
install_data(
  symbolic_dir / ('@0@-symbolic.svg').format(application_id),
  install_dir: get_option('datadir') / 'icons' / symbolic_dir
)

install_dir = get_option('datadir') / 'icons' / symbolic_dir
custom_icons = []
foreach icon_file : run_command('find', symbolic_dir, '-name', '*.svg').stdout().strip().split('\n')
  if icon_file != ''
    custom_icons += files(icon_file)
  endif
endforeach
install_data(custom_icons, install_dir: install_dir)
