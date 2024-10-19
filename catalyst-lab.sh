#!/bin/bash

# Check for root privilages.
if [[ $EUID -ne 0 ]]; then
	echo "This script must be run as root"
	exit 1
fi

# Possible variables stored in stages array in this script.
declare STAGE_KEYS=(available_build catalyst_conf catalyst_conf_src cpu_flags children overlays parent platform rebuild release releng_base selected source_subpath stage subarch target version_stamp)

declare -A TARGET_MAPPINGS=(
	# Used to fill spec fsscript and similar with correct key.
	[livecd-stage1]=livecd
	[livecd-stage2]=livecd
)
declare -A ARCH_MAPPINGS=(
	# Map from arch command to base arch. TODO: Add more mappings if needed.
	[aarch64]=arm64
	[x86_64]=amd64
)
declare -A ARCH_FAMILIES=(
	# Map from base arch to arch family. Add only if different than base arch.
	[ppc64]=ppc
)
declare -A ARCH_INTERPRETERS=(
	# Add custom interpreters if needed.
	# By default script will try to find interpreter by matching with basename->raw_basename (ie x86_64).
	# This can also be used if multiple interpreters are needed.
	# Keys should correspond to baseraw, for example x86_64, aarch64, ppc64
	[x86_64]="/usr/bin/qemu-x86_64 /usr/bin/qemu-i386"
)

readonly host_arch=${ARCH_MAPPINGS[$(arch)]:-$(arch)} # Mapped to release arch
readonly timestamp=$(date -u +"%Y%m%dT%H%M%SZ") # Current timestamp.
readonly qemu_has_static_user=$(grep -q static-user /var/db/pkg/app-emulation/qemu-*/USE && echo true || echo false)
readonly qemu_binfmt_is_running=$( { [ -x /etc/init.d/qemu-binfmt ] && /etc/init.d/qemu-binfmt status | grep -q started; } || { pidof systemd >/dev/null && systemctl is-active --quiet qemu-binfmt; } && echo true || echo false )

readonly color_gray='\033[0;90m'
readonly color_red='\033[0;31m'
readonly color_green='\033[0;32m'
readonly color_turquoise='\033[0;36m'
readonly color_turquoise_bold='\033[1;36m'
readonly color_nc='\033[0m' # No Color

# Load/create config.
if [[ ! -f /etc/catalyst-lab/catalyst-lab.conf ]]; then
	# Create default config if not available
	mkdir -p /etc/catalyst-lab
	mkdir -p /etc/catalyst-lab/templates
	cat <<EOF | tee /etc/catalyst-lab/catalyst-lab.conf > /dev/null || exit 1
# Main configuration for catalyst-lab.
seeds_url=https://gentoo.osuosl.org/releases/@ARCH_FAMILY@/autobuilds
templates_path=/etc/catalyst-lab/templates
releng_path=/opt/releng
catalyst_path=/var/tmp/catalyst
catalyst_usr_path=/usr/share/catalyst
pkgcache_base_path=/var/cache/catalyst-binpkgs
tmp_path=/tmp/catalyst-lab
jobs=$(nproc)
load_average=$(nproc).0
EOF
	echo "Default config file created: /etc/catalyst-lab/catalyst-lab.conf"
	echo ""
fi
source /etc/catalyst-lab/catalyst-lab.conf

readonly work_path=${tmp_path}/${timestamp}
readonly catalyst_builds_path=${catalyst_path}/builds

# Create required folders if don't exists
if [[ ! -d ${catalyst_builds_path} ]]; then
	mkdir -p ${catalyst_builds_path}
fi

# Script arguments:
declare -a selected_stages_templates
while [ $# -gt 0 ]; do case ${1} in
	--update-snapshot) FETCH_FRESH_SNAPSHOT=true;;
	--update-releng) FETCH_FRESH_RELENG=true;;
	--update-repos) FETCH_FRESH_REPOS=true;;
	--clean) CLEAN_BUILD=true;; # Perform clean build - don't use any existing sources even if available (Except for downloaded seeds).
	--build) BUILD=true; PREPARE=true;; # Prepare is implicit when using --build.
	--prepare) PREPARE=true;;
	--*) echo "Unknown option ${1}"; exit;;
	-*) echo "Unknown option ${1}"; exit;;
	*) selected_stages_templates+=("${1}");;
esac; shift; done

# Functions:

# Clean tmp files if exited with error.
cleanup() {
	if [[ $? -ne 0 ]]; then
		# Cleanup build directory.
		# rm -rf ${work_path} # Leave for future analysis.
		# Cleanup temporary toml file.
		if [[ -n ${toml_file} ]] && [[ -f ${toml_file} ]]; then
			rm -f ${toml_file}
		fi
	fi
}

echo_color() { # Usage: echo_color COLOR MESSAGE
	echo -e "${1}${2}${color_nc}"
}

contains_string() {
	local array=("${!1}")
	local search_string="$2"
	local found=0

	for element in "${array[@]}"; do
		if [[ "$element" == "$search_string" ]]; then
			found=1
			break
		fi
	done

	if [[ $found -eq 1 ]]; then
		return 0  # true
	else
		return 1  # false
	fi
}

# Get list of directories in given directory.
get_directories() {
	local path=${1}
	local directories=($(find ${path}/ -mindepth 1 -maxdepth 1 -type d -exec basename {} \; | sort))
	echo ${directories[@]}
}

basearch_to_baseraw() {
	local basearch=${1}
	for key in ${!ARCH_MAPPINGS[@]}; do
		if [[ ${ARCH_MAPPINGS[${key}]} == ${basearch} ]]; then
			echo ${key}
			return
		fi
	done
	echo ${basearch}
}

# Read variables of stage at index. Use this in functions that sould work with given stage, instead of loading all variables manually.
# Use prefix if need to compare with other stage variables.
# This function also loads platform config file related to selected stage.
use_stage() {
	local prefix=${2}
	# Automatically determine all possible keys stored in stages, and load them to variables.
	#local keys=($(printf "%s\n" "${!stages[@]}" | sed 's/.*,//' | sort -u))
	for variable in ${STAGE_KEYS[@]}; do
		local value=${stages[${1},${variable}]}
		eval "${prefix}${variable}='${value}'"
	done
	# Load parent info and platform config
	if [[ -z ${prefix} ]]; then
		parent_index=""
		local i; for (( i=0; i<${stages_count}; i++ )); do
			use_stage ${i} parent_
# TODO: Take into consideration customized rel_type in parent spec
			local parent_product=${parent_platform}/${parent_release}/${parent_target}-${parent_subarch}-${parent_version_stamp}
			if [[ ${source_subpath} == ${parent_product} ]]; then
				parent_index=${i}
				break
			fi
		done
		if [[ -z ${parent_index} ]]; then # If parent not found, clean it's data
			for variable in ${keys[@]}; do
				unset parent_${variable}
			done
		fi

		# Platform config
		# If some properties are not set in config - unset them while loading new config
		unset repos common_flags chost toml cpu_flags
		local platform_conf_path=${templates_path}/${platform}/platform.conf
		source ${platform_conf_path}
		local release_conf_path=${templates_path}/${platform}/${release}/release.conf
		if [[ -f ${release_conf_path} ]]; then
			# Variables in release.conf can overwrite platform defaults.
			source ${release_conf_path}
		fi
		arch_basearch=${arch%%/*}
		arch_baseraw=$(basearch_to_baseraw ${arch_basearch})
		arch_subarch=${arch#*/}; arch_subarch=${arch_subarch:-$arch_basearch}
		arch_family=${ARCH_FAMILIES[${arch_basearch}]:-${arch_basearch}}
		arch_interpreter=${ARCH_INTERPRETERS[${arch_baseraw}]:-"/usr/bin/qemu-${arch_baseraw}"} # Find correct arch_interpreter
	fi
}

# Return value of given property from given spec file.
read_spec_variable() {
	local spec_path=${1}
	local variable_name=${2}
	# get variable from spec file and trim whitespaces.
	local value=$(cat ${spec_path} | sed -n "/^${variable_name}:/s/^${variable_name}:\(.*\)/\1/p" | tr -d '[:space:]')
	echo ${value}
}

# Update value in given spec or add if it's not present there
set_spec_variable() {
	local spec_path=${1}
	local key=${2}
	local new_value="${3}"
	if grep -q "^$key:" ${spec_path}; then
		sed -i "s|^$key: .*|$key: $new_value|" ${spec_path}
	else
		echo "$key: $new_value" >> ${spec_path}
	fi
}

# Set variable in spec only if it's not specified yet.
# Use this for example for treeish - you can sepcify selected one or leave it out to get automatic value.
set_spec_variable_if_missing() {
	local spec_path=${1}
	local key=${2}
	local new_value="${3}"
	if ! grep -q "^$key:" "${spec_path}"; then
		echo "$key: $new_value" >> "${spec_path}"
	fi
}

# Fill tmp data in spec (@TIMESTAMP@, etc)
update_spec_variable() {
	local spec_path=${1}
	local key=${2}
	local new_value="${3}"
	sed -i "s|@${key}@|${new_value}|g" ${spec_path}
}

# Replace variables in given stage variable, by replacing some strings with calculated end results - timestamp, PLATFORM, STAGE.
sanitize_spec_variable() {
	local platform="$1"
	local release="$2"
	local stage="$3"
	local family="$4"
	local base_arch="$5"
	local sub_arch="$6"
	local value="$7"
	echo "${value}" | sed "s/@REL_TYPE@/${release}/g" | sed "s/@PLATFORM@/${platform}/g" | sed "s/@STAGE@/${stage}/g" | sed "s/@BASE_ARCH@/${base_arch}/g" | sed "s/@SUB_ARCH@/${sub_arch}/g" | sed "s/@FAMILY_ARCH@/${family}/g"
}

#  Get portage snapshot version and download new if needed.
prepare_portage_snapshot() {
	if [[ -d ${catalyst_path}/snapshots && $(find ${catalyst_path}/snapshots -type f -name "*.sqfs" | wc -l) -gt 0 ]]; then
		treeish=$(find ${catalyst_path}/snapshots -type f -name "*.sqfs" -exec ls -t {} + | head -n 1 | xargs -n 1 basename -s .sqfs | cut -d '-' -f 2)
	fi
	if [[ -z ${treeish} ]] || [[ ${FETCH_FRESH_SNAPSHOT} = true ]]; then
		echo_color ${color_turquoise_bold} "[ Refreshing portage snapshot ]"
		catalyst -s stable
		treeish=$(find ${catalyst_path}/snapshots -type f -name "*.sqfs" -exec ls -t {} + | head -n 1 | xargs -n 1 basename -s .sqfs | cut -d '-' -f 2)
		echo "" # New line
	fi
}

# Get latest releng release if needed.
prepare_releng() {
	# If releng directory doesn't exists - download new version
	# If it exists and FETCH_FRESH_RELENG is set, pull changes.
	if [[ ! -d ${releng_path} ]]; then
		echo_color ${color_turquoise_bold} "[ Downloading releng ]"
		git clone https://github.com/gentoo/releng.git ${releng_path} || exit 1
		echo ""
	elif [[ ${FETCH_FRESH_RELENG} = true ]]; then
		echo_color ${color_turquoise_bold} "[ Updating releng ]"
		git -C ${releng_path} pull || exit 1
		echo ""
	fi
}

# Load list of stages to build for every platform and release.
# Prepare variables of every stage, including changes from sanitization process.
# Sort stages based on their inheritance.
load_stages() {
	declare -gA stages # Some details of stages retreived from scanning. (release,stage,target,source,has_parent).
	available_builds=$(find ${catalyst_builds_path} -type f -name "*.tar.xz" -printf '%P\n')
	stages_count=0 # Number of stages to build. Script will determine this value automatically.

	readonly RL_VAL_PLATFORMS=$(get_directories ${templates_path})
	for platform in ${RL_VAL_PLATFORMS[@]}; do
		local platform_path=${templates_path}/${platform}
		# Load platform config
		unset repos common_flags chost toml cpu_flags
		local platform_conf_path=${templates_path}/${platform}/platform.conf
		source ${platform_conf_path}
		local arch_basearch=${arch%%/*}
		local arch_baseraw=$(basearch_to_baseraw ${arch_basearch})
		local arch_subarch=${arch#*/}; arch_subarch=${arch_subarch:-$arch_basearch}
		local arch_family=${ARCH_FAMILIES[${arch_basearch}]:-${arch_basearch}}
		local arch_interpreter=${ARCH_INTERPRETERS[${arch_baseraw}]:-"/usr/bin/qemu-${arch_baseraw}"} # Find correct arch_interpreter
		# Find list of releases. (23.0-default, 23.0-llvm, etc).
		RL_VAL_RELEASES=$(get_directories ${platform_path})
		# Collect information about stages in releases.
		for release in ${RL_VAL_RELEASES[@]}; do
			# (data/templates/23.0-default)
			local release_path=${platform_path}/${release}
			# Find list of stages in current releass. (stage1-cell-base-openrc stage3-cell-base-openrc, ...)
			RL_VAL_RELEASE_STAGES=$(get_directories ${release_path})
			for stage in ${RL_VAL_RELEASE_STAGES[@]}; do
				# (data/templates/23.0-default/stage1-openrc-cell-base)
				local stage_path=${templates_path}/${platform}/${release}/${stage}
				# (data/templates/23.0-default/stage1-openrc-cell-base/stage.spec)
				local stage_spec_path=${stage_path}/stage.spec
				if [[ -f ${stage_spec_path} ]]; then
					local subarch=$(read_spec_variable ${stage_spec_path} subarch) # eq.: cell
					local target=$(read_spec_variable ${stage_spec_path} target) # eq.: stage3
					local version_stamp=$(read_spec_variable ${stage_spec_path} version_stamp) # eq.: base-openrc-@TIMESTAMP@
					local source_subpath=$(read_spec_variable ${stage_spec_path} source_subpath)
					local spec_repos=$(read_spec_variable ${stage_spec_path} repos)
					local releng_base=$(read_spec_variable ${stage_spec_path} releng_base)

					# If subarch is not set in spec, update it with value from platform config.
					if [[ -z ${subarch} ]]; then
						subarch=${arch_subarch}
					fi

					# Find best matching local build available.
					local stage_product=${platform}/${release}/${target}-${subarch}-${version_stamp}
					local stage_product_regex=$(echo $(sanitize_spec_variable ${platform} ${release} ${stage} ${arch_family} ${arch_basearch} ${subarch} ${stage_product}) | sed 's/@TIMESTAMP@/[0-9]{8}T[0-9]{6}Z/')
					local matching_stage_builds=($(printf "%s\n" "${available_builds[@]}" | grep -E "${stage_product_regex}"))
					local stage_available_build=$(printf "%s\n" "${matching_stage_builds[@]}" | sort -r | head -n 1)

					# Store variables
					stages[${stages_count},platform]=${platform}
					stages[${stages_count},release]=${release}
					stages[${stages_count},stage]=${stage}
					stages[${stages_count},subarch]=$(sanitize_spec_variable ${platform} ${release} ${stage} ${arch_family} ${arch_basearch} ${subarch} ${subarch})
					stages[${stages_count},target]=$(sanitize_spec_variable ${platform} ${release} ${stage} ${arch_family} ${arch_basearch} ${subarch} ${target})
					stages[${stages_count},version_stamp]=$(sanitize_spec_variable ${platform} ${release} ${stage} ${arch_family} ${arch_basearch} ${subarch} ${version_stamp})
					stages[${stages_count},source_subpath]=$(sanitize_spec_variable ${platform} ${release} ${stage} ${arch_family} ${arch_basearch} ${subarch} ${source_subpath})
					stages[${stages_count},overlays]=${spec_repos:-${repos}}
					stages[${stages_count},releng_base]=${releng_base}
					stages[${stages_count},available_build]=${stage_available_build}

					stages_count=$((stages_count + 1))

				fi
			done
		done
	done

	# Sort stages array by inheritance:
	stages_order=() # Order in which stages should be build, for inheritance to work. (1,5,2,0,...).
	# Prepare stages order by inheritance.
	local i; for (( i=0; i<${stages_count}; i++ )); do
		insert_stage_with_inheritance ${i}
	done
	# Sort stages by inheritance order in temp array..
	declare -A stages_temp
	local i; for (( i=0; i<${stages_count}; i++ )); do
		local index=${stages_order[${i}]}
		for key in ${!stages[@]}; do
			if [[ ${key} == ${index},* ]]; then
				field=${key#*,}
				stages_temp[${i},${field}]=${stages[${key}]}
			fi
		done
	done
	# Write sorted array back to stages array.
	local i; for (( i=0; i<${stages_count}; i++ )); do
		for key in ${!stages_temp[@]}; do
			if [[ ${key} == ${i},* ]]; then
				field=${key#*,}
				stages[${i},${field}]=${stages_temp[$key]}
			fi
		done
	done
	unset stages_order
	unset stages_temp

	# Determine if state needs to be rebuilt or can local build be used instead.
	# Depends on selected stages and local builds availibility.
	if [[ ${#selected_stages_templates[@]} -eq 0 ]]; then
		# If no specific builds were selected, it means that all should be rebuild.
		local i; for (( i=0; i<${stages_count}; i++ )); do
			stages[${i},rebuild]=true
			stages[${i},selected]=true
		done
	else
		set -f
		local required_seeds=() # List of stages that are needed to be build, as stage for another required stages
		# If specified list of stages - build only if listed or if it's needed by another stage and has no local build available.
		local i; for (( i=(( ${stages_count} - 1 )); i>=0; i-- )); do # Go in reverse order, to find required parent seeds too
			use_stage ${i}
			local stage_subpath=${platform}/${release}/${stage}
			unset include
			for pattern in ${selected_stages_templates[@]}; do
				IFS='/' read -r exp_platform exp_release exp_stage <<< ${pattern}
				unset fits_stage; unset fits_release; unset fits_platform
				if [[ -z ${exp_stage} ]] || [[ ${stage} == ${exp_stage} ]]; then
					local fits_stage=true
				fi
				if ( [[ -z ${exp_stage} ]] && [[ -z ${exp_release} ]] ) || [[ ${release} == ${exp_release} ]]; then
					local fits_release=true
				fi
				if ( [[ -z ${exp_stage} ]] && [[ -z ${exp_release} ]] && [[ -z ${exp_platform} ]] ) || [[ ${platform} == ${exp_platform} ]]; then
					local fits_platform=true
				fi
				local should_include=$([[ ${fits_platform} == true && ${fits_release} == true && ${fits_stage} == true ]] && echo true || echo false)
				if [[ ${should_include} = true ]]; then
					local include=true
					break
				fi
			done
			if [[ ${include} = true ]]; then
				stages[${i},rebuild]=true
				stages[${i},selected]=true
				required_seeds+=(${parent}) # Remember that current stage source need's to exist or be build.
			else
				# Check if this stage is required as a source for another stage.
				# In this situation it's marked as required only if local build is also not available or CLEAN_BUILD is set.
				if contains_string required_seeds[@] ${stage_subpath} && [[ -z ${available_build} || ${CLEAN_BUILD} = true ]]; then
					stages[${i},rebuild]=true
					stages[${i},available_build]="" # If rebuilding, forget about available build subpath, as new release will be created anyway.
					required_seeds+=(${parent}) # Remember that current stage source need's to exist or be build.
				else
					stages[${i},rebuild]=false
				fi
			fi
		done
		set +f
	fi

	# Build stages tree (determine children property)
	local i; for (( i=0; i<${stages_count}; i++ )); do
		use_stage ${i}
		if [[ ${rebuild} = false ]]; then
			continue
		fi

		# Store child in parent's childs, to build a dependency tree.
		if [[ -n ${parent_index} ]]; then
			stages[${parent_index},children]="${stages[${parent_index},children]} ${i}"
		fi
	done

	# List stages to build
	echo_color ${color_turquoise_bold} "[ Stages to rebuild ]"
	draw_stages_tree
	echo ""
}

# Prepare array that describes the order of stages based on inheritance.
# Store information if stage has local parents.
# This is function uses requrency to process all required parents before selected stage is processed.
insert_stage_with_inheritance() { # arg - index, required_by_id
	local index=${1}
	local dependency_stack=${2:-'|'}
	use_stage ${index}
	if ! contains_string stages_order[@] ${index}; then
		# If you can find a parent that produces target = this.source, add this parent first. After that add this stage.
		if [[ -n ${parent_index} ]]; then
			if [[ ${dependency_stack} == *"|${parent_index}|"* ]]; then
				dependency_stack="${dependency_stack}${index}|"
				echo "Circular dependency detected for ${parent_platform}/${parent_release}/${parent_stage}. Verify your templates."
				IFS='|' read -r -a dependency_indexes <<< "${dependency_stack#|}"
				echo "Stack:"
				local found_parent=false
				for i in ${dependency_indexes[@]}; do
					if [[ ${found_parent} = false ]] && [[ ${parent_index} != ${i} ]]; then
						continue
					fi
					found_parent=true
					echo ${stages[${i},platform]}/${stages[${i},release]}/${stages[${i},stage]}
				done
				exit 1
			fi
			stages[${index},parent]=${parent_platform}/${parent_release}/${parent_stage}
			local next_dependency_stack="${dependency_stack}${index}|"
			insert_stage_with_inheritance ${parent_index} "${next_dependency_stack}"
		else
			stages[${index},parent]=""
		fi
		stages_order+=(${index})
	fi
}

# Setup templates of stages.
configure_stages() {
	echo_color ${color_turquoise_bold} "[ Preparing stages ]"

	local i; for (( i=0; i<${stages_count}; i++ )); do
		use_stage ${i}
		if [[ ${rebuild} = false ]]; then
			continue
		fi

		local platform_path=${templates_path}/${platform}
		local release_path=${platform_path}/${release}
		local stage_path=${release_path}/${stage}

		local platform_work_path=${work_path}/${platform}
		local release_work_path=${platform_work_path}/${release}
		local stage_work_path=${release_work_path}/${stage}

		local source_build_path=${catalyst_builds_path}/${source_subpath}.tar.xz

		# Determine if stage's parent will also be rebuild, to know if it should use available_source_subpath or new parent build.
		if [[ -n ${parent_index} ]] && [[ ${parent_rebuild} = false ]] && [[ -n ${parent_available_build} ]]; then
			echo_color ${color_turquoise} "Using existing source ${parent_available_build} for ${platform}/${release}/${stage}"
			source_subpath=${parent_available_build%.tar.xz}
			stages[${i},source_subpath]=${source_subpath}
		fi

		# Check if should download seed and download if needed.
		local use_remote_build=false
		if [[ -z ${parent} ]]; then
			if [[ ! -f ${source_build_path} ]]; then
				use_remote_build=true
			fi
		fi

		# Get seed URL if needed.
		if [[ ${use_remote_build} = true ]]; then
			local source_target_stripped=$(echo ${source_subpath} | awk -F '/' '{print $NF}' | sed 's/-@TIMESTAMP@//')
			local source_target_regex=$(echo ${source_subpath} | awk -F '/' '{print $NF}' | sed 's/@TIMESTAMP@/[0-9]{8}T[0-9]{6}Z/')
			# Check if build for this seed exists already, only if building specified list of stages (otherwise always get latest details).
			local matching_source_builds=($(printf "%s\n" "${available_builds[@]}" | grep -E "${source_target_regex}"))
			local source_available_build=$(printf "%s\n" "${matching_source_builds[@]}" | sort -r | head -n 1)
			if [[ ${#selected_stages_templates[@]} -ne 0 ]] && [[ -n ${source_available_build} ]] && [[ ! ${CLEAN_BUILD} = true ]]; then
				echo_color ${color_turquoise} "Using existing source ${source_available_build} for ${platform}/${release}/${stage}"
				source_subpath=${source_available_build%.tar.xz}
				stages[${i},source_subpath]=${source_subpath}
			else
				# Download seed info for ${source_subpath}
				echo_color ${color_turquoise} "Getting seed info: ${platform}/${release}/${stage}"
				local seeds_arch_url=$(echo ${seeds_url} | sed "s/@ARCH_FAMILY@/${arch_family}/")
				local metadata_url=${seeds_arch_url}/latest-${source_target_stripped}.txt
				local metadata_content=$(wget -q -O - ${metadata_url} --no-http-keep-alive --no-cache --no-cookies)
				local latest_seed=$(echo "${metadata_content}" | grep -E ${source_target_regex} | head -n 1 | cut -d ' ' -f 1)
				local url_seed_tarball=${seeds_arch_url}/${latest_seed}
				# Extract available timestamp from available seed name and update @TIMESTAMP@ in source_subpath with it.
				local latest_seed_timestamp=$(echo ${latest_seed} | sed -n 's/.*\([0-9]\{8\}T[0-9]\{6\}Z\).*/\1/p')
				source_url=${url_seed_tarball}
				source_subpath=$(echo ${source_subpath} | sed "s/@TIMESTAMP@/${latest_seed_timestamp}/")
				stages[${i},source_url]=${source_url} # Store URL of source, to download right before build
				stages[${i},source_subpath]=${source_subpath}
				# If getting parent url fails, stop script with erro
				if [[ -z ${latest_seed} ]]; then
					echo "Failed to get seed URL for ${source_subpath}"
					exit 1
				fi
			fi
		fi

		# Prepare repos information
		if [[ -n ${overlays} ]]; then
			local repos_list
			IFS=',' read -ra repos_list <<< ${overlays}
			overlays=()
			for repo in ${repos_list[@]}; do
				if [[ ${repo} =~ ^(http|https):// || ${repo} =~ ^git@ ]]; then
					# Convert remote path to remote|local
					local local_repo_path=${tmp_path}/repos/$(echo ${repo} | sed -e 's/[^A-Za-z0-9._-]/_/g')
					overlays+=("${repo}|${local_repo_path}")
				else
					# Handle local path
					overlays+=(${repo})
				fi
			done
			overlays=$(echo ${overlays[@]} | sed 's/ /,/')
			stages[${i},overlays]=${overlays}
		fi

		# Determine if needs to use qemu interpreter.
		unset interpreter_portage_postfix
		if [[ ${host_arch} != ${arch_basearch} ]]; then
			interpreter_portage_postfix='-qemu'
			interpreter="${arch_interpreter}"
			stages[${i},interpreter]="${interpreter}"
			for interp in ${arch_interpreter}; do
				if [[ ! -f ${interp} ]]; then
					echo "Required interpreter: ${interp} is not found."
					exit 1
				fi
			done
			if [[ ${qemu_has_static_user} = false ]]; then
				echo "Qemu needs to be installed with static_user USE flag."
         				exit 1
			fi
			if [[ ${qemu_binfmt_is_running} = false ]]; then
				echo "qemu-binfmt service is not running."
				exit 1
			fi
		fi

		# Remember customized cpu_flags if set.
		if [[ -n ${cpu_flags} ]] && ( [[ ${target} == stage1 ]] || [[ ${target} == stage3 ]] ); then
			stages[${i},cpu_flags]="${cpu_flags}"
		fi

		# Find custom catalyst.conf if any
		local platform_catalyst_conf=${platform_path}/catalyst.conf
		local release_catalyst_conf=${release_path}/catalyst.conf
		local stage_catalyst_conf=${stage_path}/catalyst.conf
		local platform_work_catalyst_conf=${platform_work_path}/catalyst.conf
		local release_work_catalyst_conf=${release_work_path}/catalyst.conf
		local stage_work_catalyst_conf=${stage_work_path}/catalyst.conf
		unset catalyst_conf catalyst_conf_src
		if
		     [[ -f ${stage_catalyst_conf} ]]; then catalyst_conf_src=${stage_catalyst_conf}; catalyst_conf=${stage_work_catalyst_conf};
		elif [[ -f ${release_catalyst_conf} ]]; then catalyst_conf_src=${release_catalyst_conf}; catalyst_conf=${release_work_catalyst_conf};
		elif [[ -f ${platform_catalyst_conf} ]]; then catalyst_conf_src=${platform_catalyst_conf}; catalyst_conf=${platform_work_catalyst_conf};
		fi
		stages[${i},catalyst_conf_src]=${catalyst_conf_src}
		stages[${i},catalyst_conf]=${catalyst_conf}
	done

	echo_color ${color_green} "Stage templates prepared"
	echo ""
}

draw_stages_tree() {
	local index=${1}
	local prefix=${2}

	if [[ -z ${index} ]]; then
		# Get indexes of root elements.
		local child_array=()
		local i; for (( i=0; i < ${stages_count}; i++ )); do
			use_stage ${i}
			if [[ ${rebuild} = true ]] && ([[ -z ${parent_index} ]] || [[ ${parent_rebuild} = false ]]); then
				child_array+=(${i})
			fi
		done
	else
		use_stage ${index}
		local child_array=(${children[@]}) # Map to array if starting from string
	fi

	local i=0; for child in ${child_array[@]}; do
		((i++))
		use_stage ${child}
		local stage_name=${platform}/${release}/${color_turquoise}${stage}${color_nc}
		if [[ ${selected} = true ]]; then
			stage_name=${platform}/${release}/${color_turquoise_bold}${stage}${color_nc}
		fi
		new_prefix="${prefix}├── "
		if [[ -n ${children} ]]; then
			new_prefix="${prefix}│   "
		fi
		if [[ ${i} == ${#child_array[@]} ]]; then
			new_prefix="${prefix}    "
			echo -e "${prefix}└── ${stage_name}"
		else
			echo -e "${prefix}├── ${stage_name}"
		fi
		draw_stages_tree ${child} "${new_prefix}"
	done
}

# Save and update templates in work directory
write_stages() {
	echo_color ${color_turquoise_bold} "[ Writing stages ]"

	mkdir -p ${work_path}
	mkdir -p ${work_path}/spec_files

	local i; for (( i=0; i<${stages_count}; i++ )); do
		use_stage ${i}
		if [[ ${rebuild} = false ]]; then
			continue
		fi

		local platform_path=${templates_path}/${platform}
		local release_path=${platform_path}/${release}
		local stage_path=${release_path}/${stage}

		local platform_work_path=${work_path}/${platform}
		local release_work_path=${platform_work_path}/${release}
		local stage_work_path=${release_work_path}/${stage}

		local portage_path=${stage_work_path}/portage

		# Copy stage template workfiles to work_path.
		mkdir -p ${stage_work_path}
		cp -rf ${stage_path}/* ${stage_work_path}/

		# Create new portage_path if doesn't exists
		if [[ ! -d ${portage_path} ]]; then
			mkdir ${portage_path}
		fi

		# Prepare portage enviroment - Combine base portage files from releng with stage template portage files.
		uses_releng=false
		if [[ -n ${releng_base} ]]; then
			uses_releng=true
			releng_base_dir=${releng_path}/releases/portage/${releng_base}${interpreter_portage_postfix}
			cp -ru ${releng_base_dir}/* ${portage_path}/
		fi

		# Set 00cpu-flags file if used.
		if [[ -n ${cpu_flags} ]]; then
			local package_use_path=${portage_path}/package.use
			mkdir -p ${package_use_path}
			echo "*/* ${cpu_flags}" > ${package_use_path}/00cpu-flags
		fi

		# Find custom catalyst.conf if any
		if [[ -n ${catalyst_conf} ]]; then
			cp -n ${catalyst_conf_src} ${catalyst_conf}
			# Update NPROC value in used catalyst_conf.
			sed -i "s|@JOBS@|${jobs}|g" ${catalyst_conf}
			sed -i "s|@LOAD_AVERAGE@|${load_average}|g" ${catalyst_conf}
		fi

		# Setup spec entries.
		local stage_overlay_path=${stage_work_path}/overlay
		local stage_root_overlay_path=${stage_work_path}/root_overlay
		local stage_fsscript_path=${stage_work_path}/fsscript.sh
		local stage_spec_work_path=${stage_work_path}/stage.spec
		local target_mapping=${TARGET_MAPPINGS[${target}]:-${target}}
		local stage_default_pkgcache_path=${pkgcache_base_path}/${platform}/${release}

		# Replace spec templates with real data
		echo "" >> ${stage_spec_work_path} # Add new line, to separate new entries
		echo "# Added by catalyst-lab" >> ${stage_spec_work_path}
		set_spec_variable_if_missing ${stage_spec_work_path} rel_type ${platform}/${release}
		set_spec_variable_if_missing ${stage_spec_work_path} subarch ${arch_subarch}
		set_spec_variable_if_missing ${stage_spec_work_path} portage_confdir ${portage_path}
		set_spec_variable_if_missing ${stage_spec_work_path} snapshot_treeish ${treeish}
		set_spec_variable_if_missing ${stage_spec_work_path} pkgcache_path ${stage_default_pkgcache_path}
		set_spec_variable ${stage_spec_work_path} source_subpath ${source_subpath} # source_subpath shoud always be replaced with calculated value, to take into consideration existing old builds usage.
		if [[ -d ${stage_overlay_path} ]]; then
			set_spec_variable_if_missing ${stage_spec_work_path} ${target_mapping}/overlay ${stage_overlay_path}
		fi
		if [[ -d ${stage_root_overlay_path} ]]; then
			set_spec_variable_if_missing ${stage_spec_work_path} ${target_mapping}/root_overlay ${stage_root_overlay_path}
		fi
		if [[ -f ${stage_fsscript_path} ]]; then
			set_spec_variable_if_missing ${stage_spec_work_path} ${target_mapping}/fsscript ${stage_fsscript_path}
		fi
		if [[ -n ${interpreter} ]]; then
			set_spec_variable_if_missing ${stage_spec_work_path} interpreter "${interpreter}"
		fi
		if [[ -n ${common_flags} ]]; then
			set_spec_variable_if_missing ${stage_spec_work_path} common_flags "${common_flags}"
		fi
		if [[ -n ${chost} ]] && [[ ${target} = stage1 ]]; then # Only allow setting chost in stage1 targets.
			set_spec_variable_if_missing ${stage_spec_work_path} chost ${chost}
		fi
		if [[ -n ${overlays} ]]; then
			# Convert remote repos to local pathes, and use , to separate repos
			local repos_list
			IFS=',' read -ra repos_list <<< ${overlays}
			local repos_local_paths=()
			for repo in ${repos_list[@]}; do
				local local_path_for_remote=$(echo ${repo} | awk -F'|' '{if (NF>1) print $2; else print ""}')
				repos_local_paths+=(${local_path_for_remote:-${repo}})
			done
			repos_local_paths=$(echo ${repos_local_paths[@]} | sed 's/ /,/')
			set_spec_variable_if_missing ${stage_spec_work_path} repos ${repos_local_paths}
		fi
		if [[ ${uses_releng} = true ]]; then
			set_spec_variable_if_missing ${stage_spec_work_path} portage_prefix releng
			sed -i '/^releng_base:/d' ${stage_spec_work_path}
		fi
		update_spec_variable ${stage_spec_work_path} TIMESTAMP ${timestamp}
		update_spec_variable ${stage_spec_work_path} PLATFORM ${platform}
		update_spec_variable ${stage_spec_work_path} REL_TYPE ${release}
		update_spec_variable ${stage_spec_work_path} TREEISH ${treeish}
		update_spec_variable ${stage_spec_work_path} FAMILY_ARCH ${arch_family}
		update_spec_variable ${stage_spec_work_path} BASE_ARCH ${arch_basearch}
		update_spec_variable ${stage_spec_work_path} SUB_ARCH ${arch_subarch}
		update_spec_variable ${stage_spec_work_path} PKGCACHE_BASE_PATH ${pkgcache_base_path}

		# Create links to spec files and optionally to catalyst_conf if using custom.
		spec_link=$(echo ${work_path}/spec_files/$(printf "%03d\n" $((i + 1))).${platform}-${release}-${target}-${version_stamp} | sed "s/@TIMESTAMP@/${timestamp}/")
		ln -s ${stage_spec_work_path} ${spec_link}.spec
		if [[ -f ${catalyst_conf} ]]; then
			ln -s ${catalyst_conf} ${spec_link}.catalyst.conf
		fi
	done

	echo_color ${color_green} "Stage templates saved in: ${work_path}"
	echo ""
}

# Build stages.
build_stages() {
	echo_color ${color_turquoise_bold} "[ Building stages ]"
	local i; for (( i=0; i<${stages_count}; i++ )); do
		use_stage ${i}
		if [[ ${rebuild} = false ]]; then
			continue
		fi
		local stage_work_path=${work_path}/${platform}/${release}/${stage}
		local stage_spec_work_path=${stage_work_path}/stage.spec
		local source_path=${catalyst_builds_path}/${source_subpath}.tar.xz

		# If stage doesn't have parent built or already existing as .tar.xz, download it's
		if [[ -n ${source_url} ]] && [[ ! -f ${source_path} ]]; then
			echo_color ${color_turquoise} "Downloading seed for: ${platform}/${release}/${stage}"
			echo ""
			# Prepare stage catalyst parent build dir
			local source_build_dir=$(dirname ${source_path})
			mkdir -p ${source_build_dir}
			# Download
			wget ${source_url} -O ${source_path} || exit 1
		fi

		# Download missing remote repos
		local repos_list
		IFS=',' read -ra repos_list <<< ${overlays}
		for repo in ${repos_list[@]}; do
			local local_path_for_remote=$(echo ${repo} | awk -F '|' '{if (NF>1) print $2; else print ""}')
			if [[ -n ${local_path_for_remote} ]]; then
				local repo_url=$(echo ${repo} | cut -d '|' -f 1)
				if [[ ! -d ${local_path_for_remote} ]]; then
					echo_color ${color_turquoise} "Clonning overlay repo ${repo_url}"
					mkdir -p ${local_path_for_remote}
					git clone ${repo_url} ${local_path_for_remote}
				elif [[ ${FETCH_FRESH_REPOS} = true ]]; then
					echo_color ${color_turquoise} "Updating overlay repo ${repo_url}"
					git -C ${local_path_for_remote} pull
				fi
				echo ""
			fi
		done

		# Define custom toml file for customized archirecture.
		if [[ -n ${toml} ]]; then
			toml_file=${catalyst_usr_path}/arch/catalyst-lab.${arch_basearch}.${arch_subarch}.toml
			echo "# This file was added as a temporary file by catalyst-lab." > ${toml_file}
			echo "# It should be deleted automatically when catalyst-lab finishes building stages." >> ${toml_file}
			echo "${toml}" >> ${toml_file}
		fi

		echo_color ${color_turquoise} "Building stage: ${platform}/${release}/${stage}"
		echo ""
		local args="-af ${stage_spec_work_path}"
		if [[ -n ${catalyst_conf} ]]; then
			args="${args} -c ${catalyst_conf}"
		fi
		catalyst $args || exit 1

		# Clean temp toml file.
		if [[ -n ${toml_file} ]] && [[ -f ${toml_file} ]]; then
			rm -f ${toml_file}
		fi
		unset toml_file

		echo_color ${color_green} "Stage build completed: ${platform}/${release}/${stage}"
		echo ""
	done
}

# Main program:

# Register cleanup for script exit.
trap cleanup EXIT
trap cleanup SIGINT SIGTERM

load_stages
if [[ ${PREPARE} = true ]]; then
	prepare_portage_snapshot
	prepare_releng
	configure_stages
	write_stages
fi
if [[ ${BUILD} = true ]]; then
	build_stages
else
	echo "To build selected stages use --build flag."
fi

# TODO: Add lock file preventing multiple runs at once, but only if the same builds are involved (maybe).
# TODO: Add functions to manage platforms, releases and stages - add new, edit config, print config, etc.
# TODO: Add possibility to include shared files anywhere into spec files. So for example keep single list of basic installCD tools, and use them across all livecd specs.
# TODO: Make it possible to work with hubs (git based) - adding hub from github link, pulling automatically changes, registering in shared hub list, detecting name collisions.
# TODO: Check if settings common_flags is also only allowed in stage1
# TODO: Working with distcc (including local)
# TODO: Using remote binhosts
# TODO: Make possible setting different build sublocation (for building modified seeds)
# TODO: Correct dependencies detection when rel_type is definied in source spec
# TODO: Make things like target and timestamp infered automatically from folder name (names will have to be kept in specific format after that change)
