#!/bin/bash

# ------------------------------------------------------------------------------
# Main functions:

# Load list of stages to build for every platform and release.
# Prepare variables used by the script for every stage.
# Sort stages based on their inheritance.
# Determine which stages to build.
# Insert virtual download stages for missing seeds.
load_stages() {
	declare -gA stages # Some details of stages retreived from scanning. (release,stage,target,source,has_parent).
	stages_count=0 # Number of all stages. Script will determine this value automatically.
	available_builds_files=$(find ${catalyst_builds_path} -type f \( -name "*.tar.xz" -o -name "*.iso" \) -printf '%P\n')

	# Load basic details from platform.conf, release.conf and stage.spec files:
	# Find list of platforms. (ps3, rpi5, amd64, ...).
	readonly RL_VAL_PLATFORMS=$(get_directories ${templates_path})
	for platform in ${RL_VAL_PLATFORMS[@]}; do
		local platform_path=${templates_path}/${platform}

		# Load platform config. Saved to variables with platform_ prefix.
		for key in ${PLATFORM_KEYS[@]}; do unset ${key}; done
		source ${platform_path}/platform.conf
		for key in ${PLATFORM_KEYS[@]}; do eval platform_${key}=\${${key}}; done

		# Set platform_catalyst_conf variable for platform based on file existance.
		[[ -f ${platform_path}/catalyst.conf ]] && platform_catalyst_conf=${platform_path}/catalyst.conf || unset platform_catalyst_conf

		# Set platform arch variables (determined from platform_arch).
		local platform_basearch=${platform_arch%%/*}
		local platform_baseraw=$(basearch_to_baseraw ${platform_basearch})
		local platform_subarch=${platform_arch#*/}; platform_subarch=${platform_subarch:-${platform_basearch}}
		local platform_family=${ARCH_FAMILIES[${platform_basearch}]:-${platform_basearch}}
		local platform_interpreter=${ARCH_INTERPRETERS[${platform_baseraw}]:-/usr/bin/qemu-${platform_baseraw}} # Find correct arch_interpreter

		# Find list of releases in current platform. (23.0-default, 23.0-llvm, ...).
		RL_VAL_RELEASES=$(get_directories ${platform_path})
		for release in ${RL_VAL_RELEASES[@]}; do
			local release_path=${platform_path}/${release}

			for key in ${RELEASE_KEYS[@]}; do unset ${key}; done
			source ${release_path}/release.conf
			for key in ${RELEASE_KEYS[@]}; do eval release_${key}=\${${key}}; done

			# Set release_catalyst_conf variable for release based on file existance.
			[[ -f ${release_path}/catalyst.conf ]] && release_catalyst_conf=${release_path}/catalyst.conf || unset release_catalyst_conf

			# Find list of stages in current releass. (stage1-cell-base-openrc stage3-cell-base-openrc, ...)
			RL_VAL_RELEASE_STAGES=$(get_directories ${release_path})
			for stage in ${RL_VAL_RELEASE_STAGES[@]}; do
				local stage_path=${templates_path}/${platform}/${release}/${stage}
				local stage_info_path=${stage_path}/stage.spec

				# Set stage_catalyst_conf variable for stage based on file existance.
				[[ -f ${stage_path}/catalyst.conf ]] && stage_catalyst_conf=${stage_path}/catalyst.conf || unset stage_catalyst_conf

				# Load values stored directly in stage.spec to stage dictionary
				declare -A stage_values=(); local key=""
				# Read the file
				while IFS= read -r line || [[ -n ${line} ]]; do
					line=$(echo ${line} | sed 's/#.*//; s/[[:space:]]*$//') # Remove comments and trim trailing whitespace
					[[ -z ${line} ]] && continue # Skip empty lines
					# Check if the line contains a new key
					if [[ ${line} =~ ^([a-zA-Z0-9_-]+):[[:space:]]*(.*) ]]; then
						key=${BASH_REMATCH[1]}
						value=${BASH_REMATCH[2]}
						stage_values[${key}]="${value}"
					elif [[ -n ${key} ]]; then
						stage_values[${key}]+=" $(echo ${line} | xargs)"
					fi
				done < ${stage_info_path}
				# Trim leading/trailing spaces:
				for key in ${!stage_values[@]}; do
					local value=$(echo ${stage_values[${key}]} | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')
					stage_values[${key}]=${value}
				done

				# Prepare shared overwrites and computations of varialbles. These include properties for every possible stage type.
				# Only include here variables that might require special treament and the value will be the same for all target types.
				# If some property is different between different stage types, it will be set bellow.
				local _kind=${stage_values[kind]:-build} # If not specified, assume build.
				# Prepare variables that differ between kinds.
				if [[ ${_kind} = build ]]; then
					local _target=${stage_values[target]:-$(echo ${stage} | sed -E 's/(.*stage[0-9]+)-.*/\1/')} # Can be skipped in spec, will be determined from stage name
				elif [[ ${_kind} = download ]]; then
					local _target=${stage_values[target]:-$(echo ${stage} | sed -E 's/(.*stage[0-9]+)-.*/\1/')} # Can be skipped in spec, will be determined from stage name
				elif [[ ${_kind} = binhost ]]; then
					local _target=${stage_values[target]:-binhost}
				fi
				# Prepare variables with form shared between kinds.
				local _selected=$(is_stage_selected ${platform} ${release} ${stage})
				local _arch_emulation=$( [[ ${host_arch} = ${platform_basearch} ]] && echo false || echo true )
				local _subarch=${stage_values[subarch]:-${platform_subarch}} # Can be skipped in spec, will be determined from platform.conf
				local _repos=${stage_values[repos]:-${release_repos:-${platform_repos}}} # Can be definied in platform, release or stage (spec)
				local _cpu_flags=${stage_values[cpu_flags]:-${release_cpu_flags:-${platform_cpu_flags}}} # Can be definied in platform, release or stage (spec)
				local _releng_base=${stage_values[releng_base]:-${RELENG_BASES[${_target}]}} # Can be skipped in spec, will be determined automatically from target
				local _compression_mode=${stage_values[compression_mode]:-${release_compression_mode:-${platform_compression_mode:-pixz}}} # Can be definied in platform, release or stage (spec)
				local _catalyst_conf=${stage_values[catalyst_conf]:-${release_catalyst_conf:-${platform_catalyst_conf}}} # Can be added in platform, release or stage
				# Set and sanitize some of variables:
				local _rel_type=${stage_values[rel_type]:-${platform}/${release}}
				local _source_subpath=${stage_values[source_subpath]}
				local _binrepo=${stage_values[binrepo]:-${release_binrepo:-${platform_binrepo:-[local]${repos_cache_path}/local}}}
				local _binrepo_path=${stage_values[binrepo_path]:-${release_binrepo_path:-${platform_binrepo_path:-${_rel_type}}}}
				local _version_stamp=${stage_values[version_stamp]:-$(echo ${stage} | sed -E 's/.*(stage[0-9]+|binhost)-(.*)/\2-@TIMESTAMP@/; t; s/.*/@TIMESTAMP@/')}
				local _product=${_rel_type}/${_target}-${_subarch}-${_version_stamp}
				local _product_format=${_product} # Stays the same the whole time, containing "@TIMESTAMP@" string for later comparsions
				local _product_iso=$([[ ${_target} = livecd-stage2 ]] && echo ${stage_values[iso]:-install-${platform}-@TIMESTAMP@.iso} || echo "")
				local _product_iso_format=${_product_iso} # Stays the same the whole time, containing "@TIMESTAMP@" string for later comparsions
				local _profile=${stage_values[profile]}
				# Sanitize selected variables
				local properties_to_sanitize=(rel_type version_stamp source_subpath product product_iso binrepo binrepo_path profile product_format product_iso_format)
				for key in ${properties_to_sanitize[@]}; do
					eval "_${key}=\$(sanitize_spec_variable ${platform} ${release} ${stage} ${platform_family} ${platform_basearch} ${_subarch} \"${_rel_type}\" \"\${_${key}}\")"
				done
				# Computer after sanitization of dependencies.
				local _available_builds=($(printf "%s\n" "${available_builds_files[@]}" | grep -E $(echo ${_product} | sed 's/@TIMESTAMP@/[0-9]{8}T[0-9]{6}Z/') | sort -r))
				local _latest_build=$(echo "${_available_builds[@]}" | cut -d ' ' -f 1 | sed 's/\.tar\.xz$//') # Newest available
				local _latest_build_timestamp=$( [[ -n ${_latest_build} ]] && start_pos=$(expr index "${_product}" "@TIMESTAMP@") && echo "${_latest_build:$((start_pos - 1)):16}" )
				# Load toml file from catalyst
				load_toml ${platform_basearch} ${_subarch} # Loading some variables directly from matching toml, if not specified in stage configs.
				# Compute after loading toml.
				local _chost=${stage_values[chost]:-${release_chost:-${TOML_CACHE[${platform_basearch},${_subarch},chost]}}} # Can be definied in platform, release or stage (spec). Otherwise it's taken from catalyst toml matching architecture
				local _common_flags=${stage_values[common_flags]:-${release_common_flags:-${TOML_CACHE[${platform_basearch},${_subarch},common_flags]}}} # Can be definied in platform, release or stage (spec)
				local _use=$(echo "${platform_use} ${release_use} ${stage_values[use]}" | sed 's/ \{1,\}/ /g' | sed 's/^[[:space:]]*//; s/[[:space:]]*$//') # For USE flags, we combine all the values from platform, release and stage. Toml flags are added only for binhost, as for stages, catalyst takes care of that.
				local _use_toml="${TOML_CACHE[${platform_basearch},${_subarch},use]}"

				# Apply modified properties to stage config entry:
				# Non modified entries, directly from platform, release or stage settings:
				stages[${stages_count},platform]=${platform}
				stages[${stages_count},release]=${release}
				stages[${stages_count},stage]=${stage}
				stages[${stages_count},arch_basearch]=${platform_basearch}
				stages[${stages_count},arch_baseraw]=${platform_baseraw}
				stages[${stages_count},arch_family]=${platform_family}
				stages[${stages_count},arch_interpreter]=${platform_interpreter}
				stages[${stages_count},treeish]=${stage_values[treeish]} # At this point it could be empty. Will be set automatically later.
				stages[${stages_count},packages]=${stage_values[packages]}
				# Modified entries, that can be adjusted by the script:
				stages[${stages_count},kind]=${_kind}
				stages[${stages_count},target]=${_target}
				stages[${stages_count},source_subpath]=${_source_subpath}
				stages[${stages_count},selected]=${_selected}
				stages[${stages_count},arch_emulation]=${_arch_emulation}
				stages[${stages_count},arch_subarch]=${_subarch}
				stages[${stages_count},repos]=${_repos}
				stages[${stages_count},chost]=${_chost}
				stages[${stages_count},common_flags]=${_common_flags}
				stages[${stages_count},cpu_flags]=${_cpu_flags}
				stages[${stages_count},use]=${_use}
				stages[${stages_count},use_toml]=${_use_toml}
				stages[${stages_count},releng_base]=${_releng_base}
				stages[${stages_count},compression_mode]=${_compression_mode}
				stages[${stages_count},binrepo]=${_binrepo}
				stages[${stages_count},binrepo_path]=${_binrepo_path}
				stages[${stages_count},version_stamp]=${_version_stamp}
				stages[${stages_count},catalyst_conf]=${_catalyst_conf}
				stages[${stages_count},rel_type]=${_rel_type}
				stages[${stages_count},product]=${_product}
				stages[${stages_count},product_iso]=${_product_iso}
				stages[${stages_count},product_format]=${_product_format}
				stages[${stages_count},product_iso_format]=${_product_iso_format}
				stages[${stages_count},profile]=${_profile}
				stages[${stages_count},latest_build]=${_latest_build}
				stages[${stages_count},timestamp_latest]=${_latest_build_timestamp}

				# Increase processed stages count.
				stages_count=$((stages_count + 1))
			done
		done
	done

	# Find initial parents here and use this indexes later to calculate other parameters
	update_parent_indexes

	# Generate virtual stages to download seeds for stages without local parents.
	declare added_download_stages=()
	declare -A added_download_stages_indexes=()
	local i; for (( i=0; i<${stages_count}; i++ )); do
		# Skip for builds other than local and binhost
		[[ ${stages[${i},kind]} != build ]] && [[ ${stages[${i},kind]} != binhost ]] && continue
		# Skip if found local parent stage.
		[[ -n ${stages[${i},parent]} ]] && continue

		local seed_subpath=${stages[${i},source_subpath]}
		if ! contains_string added_download_stages[@] ${seed_subpath}; then
			added_download_stages+=(${seed_subpath})
			added_download_stages_indexes[${seed_subpath}]=${stages_count}
			stages[${stages_count},kind]=download
			stages[${stages_count},product]=${seed_subpath}
			stages[${stages_count},product_format]=${seed_subpath}
			stages[${stages_count},platform]=${stages[${i},arch_family]} # Use arch family as platform for virtual remote jobs
			stages[${stages_count},release]=gentoo # Use constant name gentoo for virtual download stages
			stages[${stages_count},stage]=$(echo ${stages[${stages_count},product]} | awk -F '/' '{print $NF}' | sed 's/-@TIMESTAMP@//') # In virtual downloads, stage is determined this way
			stages[${stages_count},target]=$(echo ${stages[${stages_count},stage]} | sed -E 's/(.*stage[0-9]+)-.*/\1/')
			local _is_selected=$(is_stage_selected ${stages[${stages_count},platform]} ${stages[${stages_count},release]} ${stages[${stages_count},stage]})
			stages[${stages_count},selected]=$(is_stage_selected ${stages[${stages_count},platform]} ${stages[${stages_count},release]} ${stages[${stages_count},stage]})
			# Find available build
			local _available_builds=($(printf "%s\n" "${available_builds_files[@]}" | grep -E $(echo ${seed_subpath} | sed 's/@TIMESTAMP@/[0-9]{8}T[0-9]{6}Z/') | sort -r))
			local _latest_build=$(echo "${_available_builds[@]}" | cut -d ' ' -f 1 | sed 's/\.tar\.xz$//')
			local _latest_build_timestamp=$( [[ -n ${_latest_build} ]] && start_pos=$(expr index "${seed_subpath}" "@TIMESTAMP@") && echo "${_latest_build:$((start_pos - 1)):16}" )
			stages[${stages_count},latest_build]=${_latest_build}
			stages[${stages_count},timestamp_latest]=${_latest_build_timestamp}
			# Save interpreter and basearch as the same as the child that this new seed produces. In theory this could result in 2 or more stages using the same source while haveing different base architecture, but this should not be the case in properly configured templates.
			stages[${stages_count},arch_interpreter]=${stages[${i},arch_interpreter]}
			stages[${stages_count},arch_basearch]=${stages[${i},arch_basearch]}
			stages[${stages_count},arch_baseraw]=${stages[${i},arch_baseraw]}
			stages[${stages_count},arch_family]=${stages[${i},arch_family]}
			# Inferred rel_type.
			stages[${stages_count},rel_type]=${stages[${stages_count},arch_family]}/${stages[${stages_count},release]}
			# Emulation mode. For download stages, it's not gonna be used, but it's still determined in case some other functionalities uses this stages directly.
			local _emulation=$( [[ ${host_arch} = ${stages[${stages_count},arch_basearch]} ]] && echo false || echo true )
			stages[${stages_count},arch_emulation]=${_emulation}
			# Generate seed information download URL
			local seeds_arch_url=$(echo ${seeds_url} | sed "s/@ARCH_FAMILY@/${stages[${stages_count},arch_family]}/")
			stages[${stages_count},url]=${seeds_arch_url}/latest-${stages[${stages_count},stage]}.txt

			((stages_count++))
		fi
		stages[${i},parent]=${added_download_stages_indexes[${seed_subpath}]}
	done
	unset added_download_stages

	# Sort stages array by inheritance:
	declare stages_order=() # Order in which stages should be build, for inheritance to work. (1,5,2,0,...).
	# Prepare stages order by inheritance.
	local i; for (( i=0; i<${stages_count}; i++ )); do
		insert_stage_with_inheritance ${i}
	done
	# Store stages by inheritance order in temp array.
	declare -A stages_temp
	local i; for (( i=0; i<${stages_count}; i++ )); do
		local index=${stages_order[${i}]}
		for key in ${STAGE_KEYS[@]}; do
			stages_temp[${i},${key}]=${stages[${index},${key}]}
		done
	done
	# Write sorted array back to stages array.
	local i; for (( i=0; i<${stages_count}; i++ )); do
		for key in ${STAGE_KEYS[@]}; do
			stages[${i},${key}]=${stages_temp[${i},${key}]}
		done
	done
	unset stages_order
	unset stages_temp

	# Refresh parent indexes after sorting array.
	update_parent_indexes

	# Determine inherited profiles
	local i; for (( i=0; i<${stages_count}; i++ )); do
		[[ -n ${stages[${i},profile]} ]] && continue
		[[ -z ${stages[${i},arch_subarch]} ]] && continue
		stages[${i},profile]=$(sanitize_spec_variable ${stages[${i},platform]} ${stages[${i},release]} ${stages[${i},stage]} ${stages[${i},arch_family]} ${stages[${i},arch_basearch]} ${stages[${i},arch_subarch]} ${stages[${i},rel_type]} $(inherit_profile ${i}))
	done

	# Determine stages children array.
	local i; for (( i=0; i<${stages_count}; i++ )); do
		local j; for (( j=((i+1)); j<${stages_count}; j++ )); do
			if [[ ${stages[${j},parent]} = ${i} ]]; then
				stages[${i},children]="${stages[${i},children]} ${j}"
			fi
		done
		# Trim white spaces
		stages[${i},children]=${stages[${i},children]#${stages[${i},children]%%[![:space:]]*}}
	done

	# Determine rebuild property.
	for ((i=$((stages_count - 1)); i>=0; i--)); do
		# If selected, then rebuild. Note: || [[ ${stages[${i},rebuild]} = true ]] is intentional, makes sure whole parent's tree get's checked.
		if [[ ${stages[${i},selected]} = true ]] || [[ ${stages[${i},rebuild]} = true ]]; then
			stages[${i},rebuild]=true
			# Also mark parent as rebuild, it there's no available previous build for it.
			local parent_index=${stages[${i},parent]}
			if [[ -n ${parent_index} ]] && ( [[ -z ${stages[${parent_index},latest_build]} ]] || [[ ${CLEAN_BUILD} = true ]] ); then
				stages[${parent_index},rebuild]=true
			fi
			continue
		elif [[ ${stages[${i},rebuild]} != true ]]; then
			stages[${i},rebuild]=false
		fi
	done

	# Determine takes_part property.
	for ((i=$((stages_count - 1)); i>=0; i--)); do
		if [[ $(is_taking_part_in_rebuild ${i}) = true ]]; then
			stages[${i},takes_part]=true
		else
			stages[${i},takes_part]=false
		fi
	done

	# List stages to build
	echo_color ${color_turquoise_bold} "[ Stages taking part in this process ]"
	draw_stages_tree
	echo ""

	# Determine timestamp_generated property - only for local for now.
	# For download this is filled after checking available download build.
	for ((i=$((stages_count - 1)); i>=0; i--)); do
		( [[ ${stages[${i},rebuild]} = false ]] || [[ ${stages[${i},kind]} = download ]] ) && continue
		stages[${i},timestamp_generated]=${timestamp}
	done

}

# Check if tools required for all rebuild stages are installed
validate_stages() {
	[[ ${DEBUG} = true ]] && echo_color ${color_turquoise_bold} "[ Stages sanity checks ]"

	# Prepare additional checks
	local required_checks=""
	for ((i=$((stages_count - 1)); i>=0; i--)); do
		[[ ${stages[${i},rebuild]} = false ]] && continue
		[[ ${stages[${i},kind]} = binhost ]] && [[ ! ${required_checks} == *"squashfs_tools_is_installed"* ]] && required_checks+="squashfs_tools_is_installed "
		if [[ ${stages[${i},arch_emulation]} = true ]]; then
			if [[ ! ${required_checks} == *"qemu_is_installed"* ]]; then
				required_checks+="qemu_is_installed qemu_has_static_user qemu_binfmt_is_running "
			fi
			# Create sanity checks for existance of all required interpreters.
			for interpreter in ${stages[${i},arch_interpreter]}; do
				local interpreter_var_name=$(echo ${interpreter} | sed 's/[\/-]/_/g')
				if [[ ! ${required_checks} == *"qemu_interpreter_installed${interpreter_var_name}"* ]]; then
					eval "qemu_interpreter_installed${interpreter_var_name}=$( [[ -f ${interpreter} ]] && echo true || echo false )"
					required_checks+="qemu_interpreter_installed${interpreter_var_name} "
				fi
			done
		fi
	done

	# Run checks.
	if [[ -n ${required_checks[@]} ]]; then
		validate_sanity_checks false "${DEBUG}" "${required_checks}"
	fi
}

#  Get portage snapshot version and download new if needed.
prepare_portage_snapshot() {
	if [[ -d ${catalyst_path}/snapshots && $(find ${catalyst_path}/snapshots -type f -name "*.sqfs" | wc -l) -gt 0 ]]; then
		treeish=$(find ${catalyst_path}/snapshots -type f -name "*.sqfs" -exec ls -t {} + | head -n 1 | xargs -n 1 basename -s .sqfs | cut -d '-' -f 2)
	fi
	if [[ -z ${treeish} ]] || [[ ${FETCH_FRESH_SNAPSHOT} = true ]]; then
		echo_color ${color_turquoise_bold} "[ Refreshing portage snapshot ]"
		catalyst -s stable || exit 1
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
		git clone https://github.com/gentoo/releng.git ${releng_path} --depth 1 || (echo_color ${color_red} "Failed to clone repository. Check if you have required access." && exit 1)
		echo ""
	elif [[ ${FETCH_FRESH_RELENG} = true ]]; then
		echo_color ${color_turquoise_bold} "[ Updating releng ]"
		git -C ${releng_path} pull || (echo_color ${color_red} "Failed to pull repository. Check if you have required access." && exit 1)
		echo ""
	fi
}

fetch_repos() {
	echo_color ${color_turquoise_bold} "[ Preparing remote repositories ]"

	# Collect repositories to fetch from repos, release_repos and binrepos
	local all_repos=()
	local i; for (( i=0; i<${stages_count}; i++ )); do
		[[ ${stages[${i},rebuild]} = false ]] && continue
		local stage_repos=(${stages[${i},repos]} ${stages[${i},binrepo]})
		for repo in ${stage_repos[@]}; do
			if ! contains_string all_repos[@] ${repo}; then
				all_repos+=(${repo})
			fi
		done
	done

	# Fetch remote repos
	local handled_repos=()
	for repo in ${all_repos[@]}; do
		local repo_kind=$(repo_kind ${repo})
		local repo_local_path=$(repo_local_path ${repo})
		local repo_url=$(repo_url ${repo})
		contains_string handled_repos[@] ${repo_local_path} && continue
		handled_repos+=(${repo_local_path})
		# Check if is remote repository and process in correct way
		case ${repo_kind} in
			git)
				if [[ ! -d ${repo_local_path}/.git ]]; then
					# If location doesn't exists yet - clone repository
					echo -e "${color_turquoise}Clonning repo: ${color_yellow}${repo_url}${color_nc}"
					mkdir -p ${repo_local_path}
					git clone ${repo_url} ${repo_local_path} --depth 1 || (echo_color ${color_red} "Failed to clone repository. Check if you have required access." && exit 1)
					echo ""
				elif [[ ${FETCH_FRESH_REPOS} = true ]]; then
					# If it exists - pull repository
					echo -e "${color_turquoise}Pulling repo: ${color_yellow}${repo_url}${color_nc}"
					git -C ${repo_local_path} pull || (echo_color ${color_red} "Failed to pull repository. Check if you have required access." && exit 1)
					echo ""
				fi
				;;
			rsync)
				echo -e "${color_turquoise}Syncing repo: ${color_yellow}${repo_url}${color_nc}"
				[[ ! -d ${repo_local_path} ]] && mkdir -p ${repo_local_path}
				rsync ${RSYNC_OPTIONS} ${ssh_username}@${repo_url}/ ${repo_local_path}/ || (echo_color ${color_red} "Failed to sync repository. Check if you have required access." && exit 1)
				echo ""
				;;
			local) ;; # Skip local binrepos
			*)
				echo_color ${color_red} "Error! Unsupported repo type: ${repo_kind} (${repo_url})"
				echo ""
				;;
		esac
	done

	echo_color ${color_green} "Remote repositories prepared"
	echo ""
}

# Setup additional information for stages:
# Final download URL's.
# Real seed names, with timestamp replaced.
prepare_stages() {
	echo_color ${color_turquoise_bold} "[ Preparing stages ]"

	local i; for (( i=0; i<${stages_count}; i++ )); do
		# Prepare only stages that needs rebuild.
		if [[ ${stages[${i},rebuild]} = true ]]; then
			# Update treeish property for local builds and binhosts.
			( [[ ${stages[${i},kind]} = build ]] || [[ ${stages[${i},kind]} = binhost ]] ) && stages[${i},treeish]=${stages[${i},treeish]:-${treeish}}

			# Prepare download builds newest timestamp and url from backend.
			if [[ ${stages[${i},kind]} = download ]]; then
				echo -e "${color_turquoise}Getting seed info: ${color_yellow}${stages[${i},platform]}/${stages[${i},release]}/${stages[${i},stage]}${color_nc}"
				local metadata_content=$(wget -q -O - ${stages[${i},url]} --no-http-keep-alive --no-cache --no-cookies)
				local stage_regex=${stages[${i},stage]}"-[0-9]{8}T[0-9]{6}Z"
				local latest_seed=$(echo "${metadata_content}" | grep -E ${stage_regex} | head -n 1 | cut -d ' ' -f 1)
				local arch_url=$(echo ${seeds_url} | sed "s/@ARCH_FAMILY@/${stages[${i},arch_family]}/")
				# Replace URL from metadata url to stage download url
				stages[${i},url]=${arch_url}/${latest_seed}
				# Extract download available timestamp and store it in stage timestamp_generated
				stages[${i},timestamp_generated]=$(echo ${latest_seed} | sed -n -r "s|.*${stages[${i},stage]}-([0-9]{8}T[0-9]{6}Z).*|\1|p")
			fi
		fi

		# Fill timestamps in stages.
		local stage_timestamp=${stages[${i},timestamp_generated]:-${stages[${i},timestamp_latest]}}
		if [[ -n ${stage_timestamp} ]]; then
			# Update stage_timestamp in product and version_stamp of this target
			stages[${i},product]=$(echo ${stages[${i},product]} | sed "s|@TIMESTAMP@|${stage_timestamp}|")
			stages[${i},product_iso]=$(echo ${stages[${i},product_iso]} | sed "s|@TIMESTAMP@|${stage_timestamp}|")
			stages[${i},version_stamp]=$(echo ${stages[${i},version_stamp]} | sed "s|@TIMESTAMP@|${stage_timestamp}|")
			# Update childrent source_subpath timestamp
			for child in ${stages[${i},children]}; do
				stages[${child},source_subpath]=$(echo ${stages[${child},source_subpath]} | sed "s|@TIMESTAMP@|${stage_timestamp}|")
			done
		fi
	done

	echo_color ${color_green} "Stage templates prepared"
	echo ""
}

# Save and update templates in work directory
write_stages() {
	echo_color ${color_turquoise_bold} "[ Writing stages ]"

	mkdir -p ${work_path}
	mkdir -p ${work_path}/jobs
	mkdir -p ${work_path}/binhosts

	local i; local j=0; for (( i=0; i<${stages_count}; i++ )); do
		[[ ${stages[${i},rebuild]} = false ]] && continue
		((j++))

		# Create stages final files - spec, catalyst.conf, portage_confdir, root_overlay, overlay, fstype and download job scripts.
		# Treat download and build jobs differently here.

		local platform_path=${templates_path}/${stages[${i},platform]}
		local platform_path_work=${work_path}/${stages[${i},platform]}
		local release_path=${platform_path}/${stages[${i},release]}
		local release_path_work=${platform_path_work}/${stages[${i},release]}
		local stage_path=${release_path}/${stages[${i},stage]}
		local stage_path_work=${release_path_work}/${stages[${i},stage]}
		local spec_link_work=$(printf ${work_path}/jobs/%03d.${stages[${i},platform]}-${stages[${i},release]}-${stages[${i},stage]} ${j})
		local portage_path_work=${stage_path_work}/portage

		# Prepare repos_local_paths
		local repos_local_paths=()
		for repo in ${stages[${i},repos]}; do
			repos_local_paths+=($(repo_local_path ${repo}))
		done

		# Copy stage template workfiles to work_path. For virtual stages, just create work directory (virtual stages don't have existing stage_path directory).
		mkdir -p ${stage_path_work}
		if [[ -d ${stage_path} ]]; then
			cp -rf ${stage_path}/* ${stage_path_work}/
		fi

		if [[ ${stages[${i},kind]} = build ]]; then

			# Setup used paths:
			local catalyst_conf_work=${stage_path_work}/catalyst.conf
			local stage_overlay_path_work=${stage_path_work}/overlay
			local stage_root_overlay_path_work=${stage_path_work}/root_overlay
			local stage_fsscript_path_work=${stage_path_work}/fsscript.sh
			local stage_info_path_work=${stage_path_work}/stage.spec
			local target_mapping=${TARGET_MAPPINGS[${stages[${i},target]}]:-${stages[${i},target]}}
			local stage_pkgcache_path=$(repo_local_path ${stages[${i},binrepo]})/${stages[${i},binrepo_path]}

			# Create new portage_work_path if doesn't exists.
			mkdir -p ${portage_path_work}

			# Prepare portage enviroment - Combine base portage files from releng with stage template portage files.
			if [[ -n ${stages[${i},releng_base]} ]]; then
				local interpreter_portage_postfix=$( [[ ${stages[${i},arch_emulation]} = true ]] && echo -qemu )
				local releng_base_dir=${releng_path}/releases/portage/${stages[${i},releng_base]}${interpreter_portage_postfix}
				cp -ru ${releng_base_dir}/* ${portage_path_work}/
			fi

			# Set 00cpu-flags file if used.
			if [[ -n ${stages[${i},cpu_flags]} ]]; then
				local package_use_path_work=${portage_path_work}/package.use
				mkdir -p ${package_use_path_work}
				echo "*/* "${stages[${i},cpu_flags]} > ${package_use_path_work}/00cpu-flags
			fi

			# Copy custom catalyst.conf if used.
			if [[ -n ${stages[${i},catalyst_conf]} ]]; then
				cp ${stages[${i},catalyst_conf]} ${catalyst_conf_work}
				# Update NPROC value in used catalyst_conf.
				sed -i "s|@JOBS@|${jobs}|g" ${catalyst_conf_work}
				sed -i "s|@LOAD_AVERAGE@|${load_average}|g" ${catalyst_conf_work}
				sed -i "s|@TMPFS_SIZE@|${tmpfs_size}|g" ${catalyst_conf_work}
			fi

			# Replace spec templates with real data:
			echo "" >> ${stage_info_path_work} # Add new line, to separate new entries
			echo "# Added by catalyst-lab" >> ${stage_info_path_work}

			set_spec_variable ${stage_info_path_work} source_subpath ${stages[${i},source_subpath]} # source_subpath shoud always be replaced with calculated value, to take into consideration existing old builds usage.

			set_spec_variable_if_missing ${stage_info_path_work} target ${stages[${i},target]}
			set_spec_variable_if_missing ${stage_info_path_work} profile ${stages[${i},profile]}
			set_spec_variable_if_missing ${stage_info_path_work} rel_type ${stages[${i},rel_type]}
			set_spec_variable_if_missing ${stage_info_path_work} subarch ${stages[${i},arch_subarch]}
			set_spec_variable_if_missing ${stage_info_path_work} version_stamp ${stages[${i},version_stamp]}
			set_spec_variable_if_missing ${stage_info_path_work} snapshot_treeish ${stages[${i},treeish]}
			set_spec_variable_if_missing ${stage_info_path_work} portage_confdir ${portage_path_work}
			set_spec_variable_if_missing ${stage_info_path_work} pkgcache_path ${stage_pkgcache_path}

			update_spec_variable ${stage_info_path_work} TIMESTAMP ${stages[${i},timestamp_generated]}
			update_spec_variable ${stage_info_path_work} PLATFORM ${stages[${i},platform]}
			update_spec_variable ${stage_info_path_work} RELEASE ${stages[${i},release]}
			update_spec_variable ${stage_info_path_work} TREEISH ${stages[${i},treeish]}
			update_spec_variable ${stage_info_path_work} FAMILY_ARCH ${stages[${i},arch_family]}
			update_spec_variable ${stage_info_path_work} BASE_ARCH ${stages[${i},arch_basearch]}
			update_spec_variable ${stage_info_path_work} SUB_ARCH ${stages[${i},arch_subarch]}
			update_spec_variable ${stage_info_path_work} PKGCACHE_BASE_PATH ${pkgcache_base_path}

			# releng portage_prefix.
			if [[ -n ${stages[${i},releng_base]} ]]; then
				set_spec_variable_if_missing ${stage_info_path_work} portage_prefix releng
			fi

			# Clear properties not used in final spec.
			local properties_to_clear=(kind releng_base)
			for key in ${properties_to_clear[@]}; do
				sed -i "/^${key}:/d" ${stage_info_path_work}
			done

			# Create customized fsscript if parent used different profile.
			if [[ ${stages[${i},target]} = stage4 ]]; then
				if [[ ! ${stages[${stages[${i},parent]},profile]} = ${stages[${i},profile]} ]] && [[ -n ${stages[${stages[${i},parent]},profile]} ]]; then
					[[ ! -f ${stage_fsscript_path_work} ]] && touch ${stage_fsscript_path_work} # Create if doesnt exists
					cat <<EOF | sed 's/^[ \t]*//g' | tee -a ${stage_fsscript_path_work} > /dev/null
						# Rebuild @world to make sure profile changes are included
						emerge --changed-use --update --deep --usepkg --buildpkg --with-bdeps=y --quiet @world
						emerge --depclean
						revdep-rebuild
EOF
				fi
			fi

			[[ -n ${stages[${i},common_flags]} ]] && set_spec_variable_if_missing ${stage_info_path_work} common_flags "${stages[${i},common_flags]}"
			[[ ${stages[${i},arch_emulation]} = true ]] && set_spec_variable_if_missing ${stage_info_path_work} interpreter "${stages[${i},arch_interpreter]}"
			[[ -d ${stage_overlay_path_work} ]] && set_spec_variable_if_missing ${stage_info_path_work} overlay ${stage_overlay_path_work}
			[[ -d ${stage_root_overlay_path_work} ]] && set_spec_variable_if_missing ${stage_info_path_work} root_overlay ${stage_root_overlay_path_work}
			[[ -f ${stage_fsscript_path_work} ]] && set_spec_variable_if_missing ${stage_info_path_work} fsscript ${stage_fsscript_path_work}
			[[ -n ${repos_local_paths} ]] && set_spec_variable_if_missing ${stage_info_path_work} repos "${repos_local_paths[@]}"

			# Special variables for only some stages:

			# Update seed.
			if [[ ${stages[${i},target]} = stage1 ]]; then
				set_spec_variable_if_missing ${stage_info_path_work} update_seed yes
				set_spec_variable_if_missing ${stage_info_path_work} update_seed_command "--changed-use --update --deep --usepkg --buildpkg --with-bdeps=y @system @world"
			fi

			# LiveCD - stage1 specific default values.
			if [[ ${stages[${i},target]} = livecd-stage1 ]]; then
				[[ -n ${stages[${i},use]} ]] && set_spec_variable ${stage_info_path_work} use "${stages[${i},use]}"
			fi

			# LiveCD - stage2 specific default values.
			if [[ ${stages[${i},target]} = livecd-stage2 ]]; then
				set_spec_variable_if_missing ${stage_info_path_work} type gentoo-release-minimal
				set_spec_variable_if_missing ${stage_info_path_work} volid Gentoo_${stages[${i},platform]}
				set_spec_variable_if_missing ${stage_info_path_work} fstype squashfs
				set_spec_variable_if_missing ${stage_info_path_work} iso ${stages[${i},product_iso]}
				[[ -n ${stages[${i},use]} ]] && set_spec_variable ${stage_info_path_work} use "${stages[${i},use]}"
			fi

			# Stage4 specific keys
			if [[ ${stages[${i},target]} = stage4 ]]; then
				set_spec_variable_if_missing ${stage_info_path_work} binrepo_path ${stages[${i},binrepo_path]}
				[[ -n ${stages[${i},use]} ]] && set_spec_variable ${stage_info_path_work} use "${stages[${i},use]}"
			fi

			if [[ -n ${stages[${i},chost]} ]] && [[ ${stages[${i},target]} = stage1 ]]; then # Only allow setting chost in stage1 targets.
				set_spec_variable_if_missing ${stage_info_path_work} chost ${stages[${i},chost]}
			fi

			if contains_string COMPRESSABLE_TARGETS[@] ${stages[${i},target]}; then
				set_spec_variable_if_missing ${stage_info_path_work} compression_mode ${stages[${i},compression_mode]}
			fi

			# Add target prefix to things like use, rcadd, unmerge, etc.
			for target_key in ${TARGET_KEYS[@]}; do
				sed -i "s|^${target_key}:|${target_mapping}/${target_key}:|" ${stage_info_path_work}
			done

			# Create links to spec files and optionally to catalyst_conf if using custom.
			ln -s ${stage_info_path_work} ${spec_link_work}.spec
			[[ -f ${catalyst_conf_work} ]] && ln -s ${catalyst_conf_work} ${spec_link_work}.catalyst.conf

		elif [[ ${stages[${i},kind]} = download ]]; then
			# Create stage for remote download:

			local download_script_path_work=${stage_path_work}/download.sh
			local download_path=${catalyst_builds_path}/${stages[${i},product]}.tar.xz
			local download_dir=$(dirname ${download_path})

			# Prepare download script for download job.
			cat <<EOF | sed 's/^[ \t]*//' | tee ${download_script_path_work} > /dev/null || exit 1
				#!/bin/bash
				file=${download_path}
				[[ -f \${file} ]] && echo 'File already exists' && exit
				mkdir -p ${download_dir}
				trap '[[ -f \${file} ]] && rm -f \${file}' EXIT INT
				wget ${stages[${i},url]} -O \${file} || exit 1
				trap - EXIT
EOF
			chmod +x ${download_script_path_work}

			# Create link to download script.
			ln -s ${download_script_path_work} ${spec_link_work}.sh

		elif [[ ${stages[${i},kind]} = binhost ]]; then
			# Create stage for building binhost packages.

			local binhost_script_path_work=${stage_path_work}/build-binpkgs.sh
			local source_tarball_path=${catalyst_builds_path}/${stages[${i},source_subpath]}.tar.xz
			local build_work_path=${work_path}/binhosts/${stages[${i},product]}
			local binrepo_path=$(repo_local_path ${stages[${i},binrepo]})/${stages[${i},binrepo_path]}

			# Create new portage_work_path if doesn't exists.
			mkdir -p ${portage_path_work}

			# Prepare portage enviroment - Combine base portage files from releng with stage template portage files.
			if [[ -n ${stages[${i},releng_base]} ]]; then
				local interpreter_portage_postfix=$( [[ ${stages[${i},arch_emulation]} = true ]] && echo -qemu )
				local releng_base_dir=${releng_path}/releases/portage/${stages[${i},releng_base]}${interpreter_portage_postfix}
				cp -ru ${releng_base_dir}/* ${portage_path_work}/
			fi

			# Set 00cpu-flags file if used.
			if [[ -n ${stages[${i},cpu_flags]} ]]; then
				local package_use_path_work=${portage_path_work}/package.use
				mkdir -p ${package_use_path_work}
				echo "*/* "${stages[${i},cpu_flags]} > ${package_use_path_work}/00cpu-flags
			fi

			# Load common_flags, use flags and chost from toml or from stage if set.
			local common_flags=${stages[${i},common_flags]}
			local chost=${stages[${i},chost]}
			local use=$(echo ${stages[${i},use_toml]} ${stages[${i},use]} | sed 's/ \{1,\}/ /g' | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')
			# TODO: Make sure that when emerging new packages, default gentoo binrepos.conf is not being used. This can probably be achieved with correct emerge flags. Still local binrepo packages should be used!

			mkdir -p ${build_work_path}

			# Prepare build script for binhost job.
			cat <<EOF | sed 's/^[ \t]*//' | tee ${binhost_script_path_work} > /dev/null || exit 1
				#!/bin/bash
				echo "(This functionality is a work in progress)"

				# Cleanup build_work_path on exit.
				trap 'echo "Cleaning: ${build_work_path}"; rm -rf ${build_work_path}' EXIT

				echo "Extract ${source_tarball_path} to ${build_work_path}"
				tar -xpf ${source_tarball_path} -C ${build_work_path} || exit 1

				if [[ ${stages[${i},arch_emulation]} = true ]]; then
					for interpreter in ${stages[${i},arch_interpreter]}; do
						echo "Inject interpreter: \${interpreter}"
						cp \${interpreter} ${build_work_path}/usr/bin/ || exit 1
					done
				fi

				echo "Preparing portage directory"
				cp -ru ${portage_path_work}/* ${build_work_path}/etc/portage/ || exit 1

				# Set common-flags, chost and use flags.
				[[ -n "${common_flags}" ]] && ( echo "Setting COMMON_FLAGS: ${common_flags}" && sed -i 's|^COMMON_FLAGS=.*$|COMMON_FLAGS="${common_flags}"|' ${build_work_path}/etc/portage/make.conf || exit 1 )
				[[ -n "${chost}" ]] && ( echo "Setting CHOST: ${chost}" && sed -i 's|^CHOST=.*$|CHOST="${chost}"|' ${build_work_path}/etc/portage/make.conf || exit 1 )
				[[ -n "${use}" ]] && echo "Setting USE flags: ${use}" && echo "USE=\\"\\\${USE} ${use}\\"" >> ${build_work_path}/etc/portage/make.conf

				# Extract portage snapshot.
				echo "Preparing portage snapshot"
				unsquashfs -d ${build_work_path}/var/db/repos/gentoo ${catalyst_path}/snapshots/gentoo-${stages[${i},treeish]}.sqfs || exit 1

				echo "Preparing chroot environment"
				unshare --mount -- bash -c ${binhost_script_path_work}-unshare || exit 1
EOF
			chmod +x ${binhost_script_path_work}

			cat <<EOF | sed 's/^[ \t]*//' | tee ${binhost_script_path_work}-unshare > /dev/null || exit 1
				# Mount necessary filesystems
				echo "Mounting system directories"
				mkdir -p ${build_work_path}/{dev,dev/pts,proc,sys,run} || exit 1
				mount -t proc /proc ${build_work_path}/proc || exit 1
				mount -t sysfs /sys ${build_work_path}/sys || exit 1
				mount -t tmpfs tmpfs ${build_work_path}/run || exit 1
				mount -t devtmpfs devtmpfs ${build_work_path}/dev || exit 1
				mount -t devpts devpts ${build_work_path}/dev/pts || exit 1

				# Bind mount binrepo to /var/cache/binpkgs to allow using and building packages for the binrepo.
				echo "Binding binhost directory: ${binrepo_path}"
				[[ ! -e ${binrepo_path} ]] && ( mkdir -p ${binrepo_path} || exit 1 ) # If binrepo path doesn't exists, create it
				mkdir -p ${build_work_path}/var/cache/binpkgs || exit 1
				mount --bind ${binrepo_path} ${build_work_path}/var/cache/binpkgs || exit 1
				# Bind overlay repos.
				repo_mount_paths=''
				if [[ -n "${repos_local_paths}" ]]; then
					mkdir -p ${build_work_path}/etc/portage/repos.conf || exit
					for repo in ${repos_local_paths[@]}; do
						echo "Binding overlay repository: \${repo}"
						# Bind repo
						repo_name=\$(basename \${repo})
						repo_mount_path=${build_work_path}/var/db/repos/\${repo_name}
						mkdir -p \${repo_mount_path} || exit 1
						mount --bind \${repo} \${repo_mount_path} || exit 1
						# Register repo
						repo_info_path=${build_work_path}/etc/portage/repos.conf/\${repo_name}.conf
						repo_real_name=\$(cat \${repo}/profiles/repo_name)
						repo_real_name=\${repo_real_name:-\${repo_name}}
						echo "[\${repo_real_name}]" > \${repo_info_path}
						echo "location = /var/db/repos/\${repo_name}" >> \${repo_info_path}
						echo "masters = gentoo" >> \${repo_info_path}
						echo "auto-sync = no" >> \${repo_info_path}
						# Remember repo to unmount
						repo_mount_paths="\${repo_mount_paths},\${repo_mount_path}"
					done
				fi

				# Bind mount resolv.conf for DNS resolution
				echo "Binding resolv.conf"
				[[ ! -f ${build_work_path}/etc/resolv.conf ]] && ( touch ${build_work_path}/etc/resolv.conf || exit 1 )
				mount --bind /etc/resolv.conf ${build_work_path}/etc/resolv.conf || exit 1

				# Trap to ensure cleanup
				trap 'umount -l ${build_work_path}/{dev/pts,dev,proc,sys,run,var/cache/binpkgs,etc/resolv.conf\${repo_mount_paths}}' EXIT

				# Insert binhost-run script into chroot and run it
				echo "Entering chroot environment"
				cp ${spec_link_work}-run.sh ${build_work_path}/run-binhost.sh || exit 1
				chroot ${build_work_path} /bin/bash -c /run-binhost.sh || exit 1
EOF
			chmod +x ${binhost_script_path_work}-unshare

			cat <<EOF | sed 's/^[ \t]*//' | tee ${binhost_script_path_work}-run > /dev/null || exit 1
				# Change profile
				if [[ -n "${stages[${i},profile]}" ]]; then
					echo "Changing profile: ${stages[${i},profile]}"
					eselect profile set ${stages[${i},profile]} || exit 1
				fi

				echo 'Searching for packages to rebuild...'
				declare packages_to_emerge=()

				for package in ${stages[${i},packages]}; do
					echo "Analyzing: \${package}"
					emerge_args=(
						--buildpkg
						--usepkg
						--getbinpkg=n
						--changed-use
						--update
						--deep
						--keep-going
					)
					output=\$(emerge \${emerge_args[@]} \${package} -pv 2>/dev/null)
					if [[ \$? -ne 0 ]]; then
						echo -e '${color_orange}Warning! '\${package}' fails to emerge. Adjust portage configuration. Skipping.${color_nc}'
						continue
					fi
					packages_to_emerge+=(\$(echo "\$output" | grep '\[ebuild.*\]' | sed -E "s/.*] ([^ ]+)(::.*)?/=\\1/"))
				done
				packages_to_emerge=(\$(echo \${packages_to_emerge[@]} | tr ' ' '\n' | sort -u | tr '\n' ' '))

				# Emerge only packages that don't have bin packages available
				if [[ \${#packages_to_emerge[@]} -gt 0 ]]; then
					echo -e 'Packages to rebuild:'
					for package in \${packages_to_emerge[@]}; do
						echo '  '\${package}
					done
					emerge_args=(
						--buildpkg
						--usepkg
						--getbinpkg=n
						--changed-use
						--update
						--deep
						--keep-going
						--quiet
						--verbose
					)
					echo "Building packages"
					echo "emerge \${emerge_args[@]} \${packages_to_emerge[@]}"
					emerge \${emerge_args[@]} \${packages_to_emerge[@]} || exit 1
					echo "All done"
				else
					echo 'Nothing to rebuild.'
				fi
EOF
			chmod +x ${binhost_script_path_work}-run

			# Create link to build script.
			ln -s ${binhost_script_path_work} ${spec_link_work}.sh
			ln -s ${binhost_script_path_work}-unshare ${spec_link_work}-unshare.sh
			ln -s ${binhost_script_path_work}-run ${spec_link_work}-run.sh

		fi
	done

	echo_color ${color_green} "Stage templates saved in: ${work_path}"
	echo ""
}

# Build stages.
build_stages() {
	echo_color ${color_turquoise_bold} "[ Building stages ]"
	local i; for (( i=0; i<${stages_count}; i++ )); do
		[[ ${stages[${i},rebuild]} = false ]] && continue

		local stage_path_work=${work_path}/${stages[${i},platform]}/${stages[${i},release]}/${stages[${i},stage]}

		if [[ ${stages[${i},kind]} = build ]]; then
			echo -e "${color_turquoise}Building stage: ${color_turquoise}${stages[${i},platform]}/${stages[${i},release]}/${stages[${i},stage]}${color_nc}"

			# Setup used paths:
			local stage_info_path_work=${stage_path_work}/stage.spec
			local catalyst_conf_work=${stage_path_work}/catalyst.conf

			local args="-af ${stage_info_path_work}"
			[[ -f ${catalyst_conf_work} ]] && args="${args} -c ${catalyst_conf_work}"

			# Perform build
			catalyst $args || exit 1

			echo -e "${color_green}Stage build completed: ${color_turquoise}${stages[${i},platform]}/${stages[${i},release]}/${stages[${i},stage]}${color_nc}"
			echo ""

		elif [[ ${stages[${i},kind]} = download ]]; then
			echo -e "${color_turquoise}Downloading stage: ${color_yellow}${stages[${i},platform]}/${stages[${i},release]}/${stages[${i},stage]}${color_nc}"

			# Setup used paths:
			local download_script_path_work=${stage_path_work}/download.sh

			# Perform build
			${download_script_path_work} || exit 1

			echo -e "${color_green}Stage download completed: ${color_yellow}${stages[${i},platform]}/${stages[${i},release]}/${stages[${i},stage]}${color_nc}"
			echo ""

		elif [[ ${stages[${i},kind]} = binhost ]]; then
			echo -e "${color_turquoise}Building packages in stage: ${color_purple}${stages[${i},platform]}/${stages[${i},release]}/${stages[${i},stage]}${color_nc}"

			# Setup used paths:
			local binhost_script_path_work=${stage_path_work}/build-binpkgs.sh

			# Perform build
			${binhost_script_path_work} || exit 1

                        echo -e "${color_green}Stage build completed: ${color_purple}${stages[${i},platform]}/${stages[${i},release]}/${stages[${i},stage]}${color_nc}"
			echo ""
		fi

	done

	echo_color ${color_green} "Stage builds completed"
	echo ""
}

upload_binrepos() {
	echo_color ${color_turquoise_bold} "[ Uploading binrepos ]"
	local handled_repos=()
	local i; for (( i=0; i<${stages_count}; i++ )); do
		[[ ${stages[${i},selected]} = true ]] || ( [[ ${stages[${i},rebuild]} = true ]] && [[ ${BUILD} = true ]] ) || continue # Only upload selected repos or rebild if building now
		contains_string handled_repos[@] ${stages[${i},binrepo]} && continue
		handled_repos+=(${stages[${i},binrepo]})
		local binrepo_kind=$(repo_kind ${stages[${i},binrepo]})
		local binrepo_local_path=$(repo_local_path ${stages[${i},binrepo]})
		local binrepo_url=$(repo_url ${stages[${i},binrepo]})

		case ${binrepo_kind} in
		git)
			[[ -d ${binrepo_local_path}/.git ]] || continue # Skip if this repo doesnt yet exists
			echo ""
			echo -e "${color_turquoise}Uploading binrepo: ${color_yellow}${stages[${i},binrepo]}${color_nc}"
			# Check if there are changes to commit
			local changes=false
			if [[ -n $(git -C ${binrepo_local_path} status --porcelain) ]]; then
				git -C ${binrepo_local_path} add -A # TODO: Only send path of binrepo_path
				git -C ${binrepo_local_path} commit -m "Automatic update: ${timestamp}"
				changes=true
			fi
			# Check if there are some commits to push
			if ! git -C ${binrepo_local_path} diff --exit-code origin/$(git -C ${binrepo_local_path} rev-parse --abbrev-ref HEAD) --quiet; then
				# Check for write access.
				if repo_url=$(git -C ${binrepo_local_path} config --get remote.origin.url) && [[ ! ${repo_url} =~ ^https:// ]] && git -C ${binrepo_local_path} ls-remote &>/dev/null && git -C ${binrepo_local_path} push --dry-run &>/dev/null; then
					git -C ${binrepo_local_path} push
				else
					echo -e "${color_orange}Warning! No write access to binrepo: ${color_yellow}${binrepo_url}${color_nc}"
				fi
				changes=true
			fi
			if [[ ${changes} = false ]]; then
				echo "No local changes detected"
			fi
			;;
		rsync)
			echo ""
			echo -e "${color_turquoise}Uploading binrepo: ${color_yellow}${binrepo_url}${color_nc}"
			rsync ${RSYNC_OPTIONS} ${binrepo_local_path}/ ${ssh_username}@${binrepo_url}/
			;;
		esac

	done
	echo ""
}

# ------------------------------------------------------------------------------
# Helper functions:

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
	local directories=($(find ${path}/ -mindepth 1 -maxdepth 1 \( -type d -o -type l -xtype d \) -exec basename {} \; | sort))
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

# Return value of given property from given spec file.
read_spec_variable() {
	local spec_path=${1}
	local variable_name=${2}
	# get variable from spec file and trim whitespaces.
	local value=$(cat ${spec_path} | sed -n "/^${variable_name}:/s/^${variable_name}:\(.*\)/\1/p" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')
	echo ${value}
}

# Update value in given spec or add if it's not present there
set_spec_variable() {
	local spec_path=${1}
	local key=${2}
	local new_value="${3}"

	if grep -q "^$key:" ${spec_path}; then
		sed -i "/^$key:/,/^[^:]*:/ {
			/^$key:/ s|^$key:.*|$key: $new_value|
			/^[^:]*:/!d
		}" ${spec_path}
	else
		echo "${key}: $new_value" >> ${spec_path}
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
	local rel_type="$7"
	local value="$8"
	echo "${value}" | sed "s|@RELEASE@|${release}|g" | sed "s|@PLATFORM@|${platform}|g" | sed "s|@STAGE@|${stage}|g" | sed "s|@BASE_ARCH@|${base_arch}|g" | sed "s|@SUB_ARCH@|${sub_arch}|g" | sed "s|@FAMILY_ARCH@|${family}|g" | sed "s|@REL_TYPE@|${rel_type}|g"
}

# Scans local and binhost targets and updates their parent property in stages array.
update_parent_indexes() {
	local i; for (( i=0; i<${stages_count}; i++ )); do
		# Search for parents only for supported stages
                if [[ ${stages[${i},kind]} != build ]] && [[ ${stages[${i},kind]} != binhost ]]; then
                        continue
                fi
		stages[${i},parent]=$(find_stage_producing ${stages[${i},source_subpath]})
	done
}

# Searches for index of a parent stage, that produces given product. Can be used to match with source_subpath
find_stage_producing() {
	local searched_product=${1}
	local j; for (( j=0; j<${stages_count}; j++ )); do
		if [[ ${searched_product} == ${stages[${j},product]} ]]; then
			echo ${j}
			return
		fi
	done
}

# Prepare array that describes the order of stages based on inheritance.
# Store information if stage has local parents.
# This is function uses requrency to process all required parents before selected stage is processed.
insert_stage_with_inheritance() { # arg - index, required_by_id
	local index=${1}
	local dependency_stack=${2:-'|'}
	if ! contains_string stages_order[@] ${index}; then
		# If you can find a parent that produces target = this.source, add this parent first. After that add this stage.
		local parent_index=${stages[${index},parent]}
		if [[ -n ${parent_index} ]]; then

			# Check for cicrular dependencies
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

			# Insert parent before current index
			local next_dependency_stack="${dependency_stack}${index}|"
			insert_stage_with_inheritance ${parent_index} "${next_dependency_stack}"
		fi
		stages_order+=(${index})
	fi
}

draw_stages_tree() {
	local index=${1}
	local prefix=${2}

	if [[ -z ${index} ]]; then
		# Get indexes of root elements.
		local child_array=()
		local i; for (( i=0; i < ${stages_count}; i++ )); do
			if [[ ${stages[${i},takes_part]} = true ]] && [[ -z ${stages[${i},parent]} ]]; then
				child_array+=(${i})
			fi
		done
	else
		# Only include branches that takes part in the process
		local child_array_tmp=(${stages[${index},children]}) # Map to array if starting from string
		local child_array=()
		for child in ${child_array_tmp[@]}; do
			if [[ ${stages[${child},takes_part]} = true ]]; then
				child_array+=(${child})
			fi
		done
	fi

	local i=0; for child in ${child_array[@]}; do
		((i++))
		if [[ ${stages[${child},kind]} = build ]]; then
			local color=${color_turquoise}
			local color_bold=${color_turquoise_bold}
		elif [[ ${stages[${child},kind]} = download ]]; then
			local color=${color_yellow}
			local color_bold=${color_yellow_bold}
		elif [[ ${stages[${child},kind]} = binhost ]]; then
			local color=${color_purple}
			local color_bold=${color_purple_bold}
		fi

		local display_name=${stages[${child},platform]}/${stages[${child},release]}/${stages[${child},stage]}
		local stage_name=${color_gray}${display_name}${color_nc}
		# If stage is not being rebuild and it has direct children that are being rebuild, display used latest_build.
		if [[ ${stages[${child},rebuild]} == false ]] && [[ -n ${stages[${child},timestamp_latest]} ]]; then
			for c in ${stages[${child},children]}; do
				if [[ ${stages[${c},rebuild]} = true ]]; then
					display_name="${display_name} (${stages[${child},timestamp_latest]})"
					stage_name=${color_gray}${display_name}${color_nc}
					break
				fi
			done
		fi
		if [[ ${stages[${child},rebuild]} = true ]]; then
			stage_name=${stages[${child},platform]}/${stages[${child},release]}/${color}${stages[${child},stage]}${color_nc}
		fi
		if [[ ${stages[${child},selected]} = true ]]; then
			stage_name=${stages[${child},platform]}/${stages[${child},release]}/${color_bold}${stages[${child},stage]}${color_nc}
		fi


		new_prefix="${prefix}├── "
		if [[ -n ${stages[${child},children]} ]]; then
			new_prefix="${prefix}│   "
		fi
		if [[ ${i} == ${#child_array[@]} ]]; then
			new_prefix="${prefix}    "
			echo -e "${prefix}└── ${stage_name}${color_nc}"
		else
			echo -e "${prefix}├── ${stage_name}${color_nc}"
		fi
		draw_stages_tree ${child} "${new_prefix}"
	done
}

# Was given stage selected by user arguments (or true if not provided.)
is_stage_selected() {
	local platform=${1}
	local release=${2}
	local stage=${3}
	if [[ ${#selected_stages_templates[@]} -eq 0 ]]; then
		echo true
		return
	fi

	set -f
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
			echo true
			set +f
			return
		fi
	done
	set +f

	echo false
}

print_debug_stack() {
	# Debug mode
	echo_color ${color_turquoise_bold} "[ Stages details ]"
	for ((i=0; i<${stages_count}; i++)); do
		echo "Stage details at index ${i}:"
		for key in ${STAGE_KEYS[@]}; do
			printf "%-22s%s\n" "${key}:" "${stages[$i,$key]}"
		done
		echo "--------------------------------------------------------------------------------"
	done
}

# Either is rebuild itself or is a part of rebuild process of it's children.
is_taking_part_in_rebuild() {
	local index=${1}
	if [[ ${stages[${index},rebuild]} = true ]]; then
		echo true
		return
	elif [[ -n ${stages[${index},children]} ]]; then
		for child in ${stages[${index},children]}; do
			if [[ $(is_taking_part_in_rebuild ${child}) = true ]]; then
				echo true
				return
			fi
		done
	fi
	echo false
}

repo_kind() {
	local repo=${1}
	# local, git, rsync
	local repo_kind=$(echo ${repo} | grep -oP '(?<=^\[)[^]]+(?=\])')
	if [[ -z ${repo_kind} ]] && ([[ ${repo} == http://* || ${repo} == https://* || ${repo} == git@* ]]); then
		# Assume git repo for URLs
		repo_kind=git
	fi
	# If type not determined yet, assume local.
	[[ -z ${repo_kind} ]] && repo_kind=local
	echo ${repo_kind}
}

# Maps repo location to local path.
# For remote addresses it maps it to local path in /var/cache.
# For local addressed it returns the same path without changes.
repo_local_path() {
	local repository=${1}
	local repo_kind=$(repo_kind ${repository})
	local repo_url=$(repo_url ${repository})

	case ${repo_kind} in
	git|rsync)
		local local_name=$(echo ${repo_url} | sed 's|http[s]*://||' | sed 's|^git@||' | sed -e 's/[^A-Za-z0-9._-]/_/g')
		echo ${repos_cache_path}/${repo_kind}_${local_name}
		;;
	local)
		echo ${repository}
		;;
	*)	# Unsupported type of repository, just return it's full value.
		echo ${repository}
		;;
	esac
}

# Removes [KIND] from repo url or local path.
repo_url() {
	local repository=${1}
	echo ${repository} | sed 's/^\[[^]]*\]//'
}

# Finds and loads catalyst toml file for specified basearch.subarch
declare -A TOML_CACHE=()
load_toml() {
	local basearch=${1}
	local subarch=${2}
	if [[ -n ${TOML_CACHE[${basearch},${subarch},chost]} ]]; then
		return # Already loaded
	fi
	local toml_file=$(grep -rl ${basearch}.${subarch} ${catalyst_usr_path}/arch)
	unset toml_chost toml_common_flags toml_use
	[[ -f ${toml_file} ]] || return
	mapfile -t use < <(tomlq -r ".${basearch}.${subarch}.USE // empty | .[]" ${toml_file})
	mapfile -t common_flags < <(tomlq ".${basearch}.${subarch}.COMMON_FLAGS // empty" ${toml_file} | sed 's/"//g')
	mapfile -t chost < <(tomlq ".${basearch}.${subarch}.CHOST // empty" ${toml_file} | sed 's/"//g')
	TOML_CACHE[${basearch},${subarch},chost]="${chost}"
	TOML_CACHE[${basearch},${subarch},common_flags]="${common_flags}"
	TOML_CACHE[${basearch},${subarch},use]="${use[*]:-""}"
}

# Validates if given sanity checks are passed
validate_sanity_checks() {
	local is_optional=${1}
	local print_success="${2}"
	local checks="${3}"
	local pass=true
	local comments=""
	for check in ${checks}; do
		if [[ ${!check} = true ]]; then
			if [[ ${print_success} = true ]]; then
				comments+="[${color_green}+${color_nc}] ${color_green}${check}${color_nc}\n"
			fi
		elif [[ ${is_optional} = true ]]; then
			comments+="[${color_orange}-${color_nc}] ${color_orange}${check}${color_nc}\n"
		else
			comments+="[${color_red}-${color_nc}] ${color_red}${check}${color_nc}\n"
			pass=false
		fi
	done
	[[ -n ${comments} ]] && comments=${comments::-2} # Remove last new line
	if [[ ! ${pass} = true ]]; then
		echo "Required sanity checks failed:"
		echo -e ${comments}
		echo "Please install and configure required tools first."
		echo "Exiting."
		exit 1
	elif [[ -n ${comments} ]]; then
		echo -e ${comments}
	fi
}

# Get profile from first parent that has it definied.
# If no parent definies profile yet, assume default/linux/@BASE_ARCH@/23.0.
inherit_profile() {
	local index=${1}
	local parent_index=${stages[${index},parent]}
	if [[ -z ${parent_index} ]]; then
		echo "default/linux/@BASE_ARCH@/23.0"
		return
	fi
	local parent_profile=${stages[${parent_index},profile]}
	if [[ -n ${parent_profile} ]]; then
		echo ${parent_profile}
	else
		inherit_profile ${parent_index}
	fi
}

# Remove old files from previous builds.
purge_old_builds_and_isos() {
	echo_color ${color_turquoise_bold} "[ Purge old builds ]"
	available_builds_files=$(find ${catalyst_builds_path} -type f \( -name "*.tar.xz" -o -name "*.iso" \) -printf '%P\n')
	local to_remove=()
	for ((i=0; i<${stages_count}; i++)); do
		[[ ! ${stages[${i},selected]} = true ]] && continue
		local _available_builds=($(printf "%s\n" "${available_builds_files[@]}" | grep -E $(echo ${stages[${i},product_format]} | sed 's/@TIMESTAMP@/[0-9]{8}T[0-9]{6}Z/' | sort))) # Sorted from oldest
		local _available_isos=($([[ -n "${stages[${i},product_iso_format]}" ]] && printf "%s\n" "${available_builds_files[@]}" | grep -E $(echo $(dirname ${stages[${i},product_format]})/${stages[${i},product_iso_format]} | sed 's/@TIMESTAMP@/[0-9]{8}T[0-9]{6}Z/') | sort))
		for ((j=0; j<((${#_available_builds[*]}-1)); j++)); do
			# Double check to make sure currently built product is not the same file
			[[ ${_available_builds[${j}]} == ${stages[${i},product]}* ]] && continue
			to_remove+=(${_available_builds[${j}]})
		done
		for ((j=0; j<((${#_available_isos[*]}-1)); j++)); do
			# Double check to make sure currently built iso is not the same file
			[[ ${_available_isos[${j}]} == ${stages[${i},product_iso]}* ]] && continue
			to_remove+=(${_available_isos[${j}]})
		done
	done
	if [[ -n ${to_remove} ]]; then
		echo "Will remove old builds:"
		for file in ${to_remove[@]}; do
			echo -e " - ${color_gray}${file}${color_nc}"
		done
		echo "Use CTRL+C to cancel."
		echo "Removing in..."
		for i in {10..0}; do
			echo -ne "\r${i} "
			sleep 1
		done
		echo ""
		for file in ${to_remove[@]}; do
			echo -e "${color_red}Removing: ${color_gray}${catalyst_builds_path}/${color_nc}${file}${color_gray}*${color_nc}"
			rm -f ${catalyst_builds_path}/${file}*
		done
	else
		echo "No old builds to remove found."
	fi
}

# ------------------------------------------------------------------------------
# START:

# Initial config

# Check for root privilages.
if [[ $EUID -ne 0 ]]; then
	echo "This script must be run as root"
	exit 1
fi

declare PLATFORM_KEYS=( # Variables allowed in platform.conf
	arch      repos
	cpu_flags compression_mode binrepo      binrepo_path
	use
)
declare RELEASE_KEYS=( # Variables allowed in release.conf
	repos            common_flags chost        cpu_flags
	compression_mode binrepo      binrepo_path
	use
)
declare STAGE_KEYS=( # Variables stored in stages[] for the script.
	kind profile

	arch_basearch arch_baseraw     arch_subarch
	arch_family   arch_interpreter arch_emulation

	platform release stage
	rel_type target version_stamp releng_base

	chost common_flags cpu_flags

	source_subpath
	parent children
	product product_format product_iso product_iso_format
	latest_build timestamp_latest timestamp_generated

	treeish
	repos
	binrepo  binrepo_path
	catalyst_conf compression_mode
	packages use use_toml

	selected rebuild takes_part

	url
)
declare TARGET_KEYS=( # Values in spec files that can be specified as <TARGET>/<VALUE>. This array is used to add <TARGET>/ automatically to these values in templates.
	use             packages unmerge      rcadd    rcdel
	rm              empty    iso          volid    fstype
	gk_mainargs     type     fsscript     groups   root_overlay
	ssh_public_keys users    linuxrc      bootargs cdtar
	depclean        fsops    modblacklist motd     overlay
	readme          verify
)
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
declare -A RELENG_BASES=(
	# Definies base releng folder for current stage.
	# This is used to prepare portage_confdir correctly for every stage,
	# while the name of releng portage subfolder is filled automatically.
	[stage1]=stages [stage2]=stages [stage3]=stages [stage4]=stages
	[livecd-stage1]=isos [livecd-stage2]=isos
	[binhost]=stages
)
# List of targets that are compressed after build. This allows adding compression_mode property automatically to stages.
declare COMPRESSABLE_TARGETS=(stage1 stage2 stage3 stage4 livecd-stage1 livecd-stage2)

readonly RSYNC_OPTIONS="--archive --delete --delete-after --omit-dir-times --delay-updates --mkpath --stats"

readonly host_arch=${ARCH_MAPPINGS[$(arch)]:-$(arch)} # Mapped to release arch
readonly timestamp=$(date -u +"%Y%m%dT%H%M%SZ") # Current timestamp.

readonly color_gray='\033[0;90m'
readonly color_red='\033[0;31m'
readonly color_green='\033[0;32m'
readonly color_turquoise='\033[0;36m'
readonly color_turquoise_bold='\033[1;36m'
readonly color_yellow='\033[0;33m'
readonly color_yellow_bold='\033[1;33m'
readonly color_orange='\033[38;5;214m'
readonly color_orange_bold='\033[1;38;5;214m'
readonly color_purple='\033[38;5;135m'
readonly color_purple_bold='\033[1;38;5;135m'
readonly color_nc='\033[0m' # No Color

# Load/create config.
if [[ ! -f /etc/catalyst-lab/catalyst-lab.conf ]]; then
	# Create default config if not available
	mkdir -p /etc/catalyst-lab
	mkdir -p /etc/catalyst-lab/templates
	cat <<EOF | sed 's/^[ \t]*//' | tee /etc/catalyst-lab/catalyst-lab.conf > /dev/null || exit 1
		# Important! Replace with your username. This value is used when downloading/uploading rsync binrepos.
		ssh_username=catalyst-lab

		tmpfs_size=6
		jobs=$(nproc)
		load_average=$(nproc).0
EOF
	echo "Default config file created: /etc/catalyst-lab/catalyst-lab.conf"
	echo ""
fi
source /etc/catalyst-lab/catalyst-lab.conf

# Constants:
readonly tmp_path=/tmp/catalyst-lab
readonly cache_path=/var/cache/catalyst-lab
readonly releng_path=/opt/releng
readonly seeds_url=https://gentoo.osuosl.org/releases/@ARCH_FAMILY@/autobuilds
readonly templates_path=/etc/catalyst-lab/templates
readonly repos_cache_path=${cache_path}/repos
readonly catalyst_path=/var/tmp/catalyst
readonly catalyst_builds_path=${catalyst_path}/builds
readonly catalyst_usr_path=/usr/share/catalyst
readonly work_path=${tmp_path}/${timestamp}

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
	--upload-binrepos) UPLOAD_BINREPOS=true;; # Try to upload changes in binrepo after build finishes.
	--purge) PURGE=true;; # Clean all builds and isos but the latest.
	--clean) CLEAN_BUILD=true;; # Perform clean build - don't use any existing sources even if available (Except for downloaded seeds).
	--build) BUILD=true; PREPARE=true;; # Prepare is implicit when using --build.
	--prepare) PREPARE=true;;
	--debug) DEBUG=true;;
	--*) echo "Unknown option ${1}"; exit;;
	-*) echo "Unknown option ${1}"; exit;;
	*) selected_stages_templates+=("${1}");;
esac; shift; done

# Main sanity check:
readonly qemu_is_installed=$( which qemu-img >/dev/null 2>&1 && echo true || echo false )
readonly qemu_has_static_user=$( ( $(ls /var/db/pkg/app-emulation/qemu-*/USE 1> /dev/null 2>&1) && grep -q static-user /var/db/pkg/app-emulation/qemu-*/USE ) && echo true || echo false)
readonly qemu_binfmt_is_running=$( { pidof systemd >/dev/null && systemctl is-active --quiet systemd-binfmt; } || { [[ -x /etc/init.d/qemu-binfmt ]] && /etc/init.d/qemu-binfmt status | grep -q started; } && echo true || echo false )
readonly catalyst_is_installed=$( which catalyst >/dev/null 2>&1 && echo true || echo false )
readonly yq_is_installed=$( which yq >/dev/null 2>&1 && echo true || echo false )
readonly git_is_installed=$( which git >/dev/null 2>&1 && echo true || echo false )
readonly squashfs_tools_is_installed=$( which mksquashfs >/dev/null 2>&1 && echo true || echo false )
readonly templates_path_exists=$( [[ -d ${templates_path} ]] && echo true || echo false )
sanity_checks_required="catalyst_is_installed yq_is_installed"
sanity_checks_optional="templates_path_exists qemu_is_installed qemu_has_static_user qemu_binfmt_is_running squashfs_tools_is_installed"
if [[ ${DEBUG} = true ]]; then
	echo_color ${color_turquoise_bold} "[ Global sanity checks ]"
fi
# Check tests required for overall script capabilities. Customized tests are also performed for stages selected to rebuild later.
validate_sanity_checks false "${DEBUG}" "${sanity_checks_required[@]}"
validate_sanity_checks true "${DEBUG}" "${sanity_checks_optional[@]}"

# ------------------------------------------------------------------------------
# Main program:

# Validate parameters
if [[ ${UPLOAD_BINREPOS} = true ]] && [[ ! ${FETCH_FRESH_REPOS} = true ]]; then
	echo "If using --upload-binrepos, it is mandatory to also use --update-repos. Exiting."
	exit 1
fi

load_stages
validate_stages
if [[ ${PREPARE} = true ]] || [[ ${FETCH_FRESH_SNAPSHOT} = true ]]; then
	prepare_portage_snapshot
fi
if [[ ${PREPARE} = true ]] || [[ ${FETCH_FRESH_RELENG} = true ]]; then
	prepare_releng
fi
if [[ ${FETCH_FRESH_REPOS} = true ]] || [[ ${PREPARE} = true ]]; then
	fetch_repos
fi
if [[ ${UPLOAD_BINREPOS} = true ]] && [[ ! ${PREPARE} = true ]]; then
	upload_binrepos
fi
if [[ ${PREPARE} = true ]]; then
	prepare_stages
	write_stages
fi
if [[ ${DEBUG} = true ]]; then
	print_debug_stack
fi
if [[ ${BUILD} = true ]]; then
	build_stages
	if [[ ${UPLOAD_BINREPOS} = true ]] && [[ ${PREPARE} = true ]]; then
		upload_binrepos
	fi
else
	if [[ ${UPLOAD_BINREPOS} = true ]] && [[ ${PREPARE} = true ]]; then
		upload_binrepos
	fi
	echo "To build selected stages use --build flag."
	echo ""
fi
if [[ ${PURGE} = true ]]; then
	purge_old_builds_and_isos
fi

# TODO: (H) When one of builds fails, mark it's children as not to build instead of breaking the whole script.
# TODO: (H) Add possibility to include shared files anywhere into spec files. So for example keep single list of basic installCD tools, and use them across all livecd specs.
# TODO: (H) Check if settings common_flags is also only allowed in stage1
# TODO: (H) Define parent property for setting source_subpath. Parent can be name of stage, full name of stage (including platform and release) or remote. With remote it can just specify word remote and automatically find version, it it can specify tarball name or even full URL.
# TODO: (H) Add possibility to define remote jobs in templates. Automatically added remote jobs are considered "virtual"
# TODO: (N) Add functions to manage platforms, releases and stages - add new, edit config, print config, etc.
# TODO: (N) Working with distcc (including local)
# TODO: (N) Add checking for valid config entries in config files
# TODO: (N) Add validation that parent and children uses the same base architecture
# TODO: (L) Make it possible to work with hubs (git based) - adding hub from github link, pulling automatically changes, registering in shared hub list, detecting name collisions.
