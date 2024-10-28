# When switching to different profile we need to rebuild world in fsscript.
# Without this old packages are not updated in stage4, as update_seed doesn't work in this stage.

emerge --changed-use --update --deep --usepkg --buildpkg --quiet @world
emerge  --depclean
revdep-rebuild
