# Define the main package
packages=(apache @world)

declare packages_to_emerge=()

echo 'Searching for packages to rebuild...'

for package in ${packages[@]}; do
	if ! emerge -p --buildpkg --usepkg --getbinpkg=n --changed-use --update --deep --keep-going --quiet ${package} >/dev/null 2>&1; then
		echo 'WARNING! '${package}' fails to emerge. Adjust portage configuration. Skipping.'
		continue
	fi
	packages_to_emerge+=($(emerge --buildpkg --usepkg --getbinpkg=n --changed-use --update --deep --keep-going ${package} -pv 2>/dev/null | grep '\[ebuild.*\]' | sed -E "s/.*] ([^ ]+)(::.*)?/=\\1/"))
done
packages_to_emerge=($(echo ${packages_to_emerge[@]} | tr ' ' '\n' | sort -u | tr '\n' ' '))

# Emerge only packages that don't have bin packages available
if [[ ${#packages_to_emerge[@]} -gt 0 ]]; then
	echo -e 'Packages to rebuild:'
	for package in ${packages_to_emerge[@]}; do
		echo '	'${package}
	done
#    emerge --ask ${packages_to_emerge[@]}
else
	echo 'Nothing to rebuild.'
fi
