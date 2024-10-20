#!/bin/bash

# ------------------------------------------------------------------------------
# Main functions:

# Load list of stages to build for every platform and release.
# Prepare variables used by the script for every stage.
# Sort stages based on their inheritance.
# Determine which stages to build.
# Insert virtual remote stages for missing seeds.
load_stages() {
	declare -gA stages # Some details of stages retreived from scanning. (release,stage,target,source,has_parent).
	available_builds=$(find ${catalyst_builds_path} -type f -name *.tar.xz -printf '%P\n')
	stages_count=0 # Number of all stages. Script will determine this value automatically.

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

			local release_keys=(repos common_flags chost cpu_flags compression_mode)
			for key in ${RELEASE_KEYS[@]}; do unset ${key}; done
			source ${release_path}/release.conf
			for key in ${RELEASE_KEYS[@]}; do eval release_${key}=\${${key}}; done

			# Set release_catalyst_conf variable for release based on file existance.
			[[ -f ${release_path}/catalyst.conf ]] && release_catalyst_conf=${release_path}/catalyst.conf || unset release_catalyst_conf

			# Find list of stages in current releass. (stage1-cell-base-openrc stage3-cell-base-openrc, ...)
			RL_VAL_RELEASE_STAGES=$(get_directories ${release_path})
			for stage in ${RL_VAL_RELEASE_STAGES[@]}; do
				local stage_path=${templates_path}/${platform}/${release}/${stage}
				local stage_spec_path=${stage_path}/stage.spec

				# Set stage_catalyst_conf variable for stage based on file existance.
				[[ -f ${stage_path}/catalyst.conf ]] && stage_catalyst_conf=${stage_path}/catalyst.conf || unset stage_catalyst_conf

				if [[ -f ${stage_spec_path} ]]; then

					# Create local stage entry and load basic data that can be retreived or calculated directly at this step.

					# Prepare static values from spec file:
					local stage_target=$(read_spec_variable ${stage_spec_path} target) # eq.: stage3
					local stage_subarch=$(read_spec_variable ${stage_spec_path} subarch) # eq.: cell
					local stage_version_stamp=$(read_spec_variable ${stage_spec_path} version_stamp) # eq.: base-openrc-@TIMESTAMP@
					local stage_source_subpath=$(read_spec_variable ${stage_spec_path} source_subpath) # Note: For builds that uses remote seeds, @TIMESTAMP@ will be later removed in this variable. But only for remotes, in local sources, it still contain @TIMESTAMP@
					local stage_repos=$(read_spec_variable ${stage_spec_path} repos)
					local stage_releng_base=$(read_spec_variable ${stage_spec_path} releng_base)
					local stage_chost=$(read_spec_variable ${stage_spec_path} chost)
					local stage_common_flags=$(read_spec_variable ${stage_spec_path} common_flags)
					local stage_cpu_flags=$(read_spec_variable ${stage_spec_path} cpu_flags)
					local stage_compression_mode=$(read_spec_variable ${stage_spec_path} compression_mode)
					local stage_treeish=$(read_spec_variable ${stage_spec_path} treeish)
					local stage_rel_type=$(read_spec_variable ${stage_spec_path} rel_type)

					# Determine final values from best possible place or calculate:
					local _kind=local
					local _platform=${platform}
					local _release=${release}
					local _stage=${stage}
					local _target=${stage_target:-$(echo ${stage} | sed -E 's/(.*stage[0-9]+)-.*/\1/')} # Can be skipped in spec, will be determined from stage name
					local _basearch=${platform_basearch}
					local _baseraw=${platform_baseraw}
					local _family=${platform_family}
					local _interpreter=${platform_interpreter} # Can be skipped in spec, will be determined from platform.conf
					local _subarch=${stage_subarch:-${platform_subarch}} # Can be skipped in spec, will be determined from platform.conf
					local _releng_base=${stage_releng_base:-${RELENG_BASES[${_target}]}} # Can be skipped in spec, will be determined automatically from target
					local _source_subpath=${stage_source_subpath}
					local _treeish=${stage_treeish} # Can be skipped in spec, will use newest seed available
					local _repos=${stage_repos:-${release_repos:-${platform_repos}}} # Can be definied in platform, release or stage (spec)
					local _chost=${stage_chost:-${release_chost:-${platform_chost}}} # Can be definied in platform, release or stage (spec)
					local _cpu_flags=${stage_cpu_flags:-${release_cpu_flags:-${platform_cpu_flags}}} # Can be definied in platform, release or stage (spec)
					local _common_flags=${stage_common_flags:-${release_common_flags:-${platform_common_flags}}} # Can be definied in platform, release or stage (spec)
					local _compression_mode=${stage_compression_mode:-${release_compression_mode:-${platform_compression_mode:-pixz}}} # Can be definied in platform, release or stage (spec)
					local _version_stamp=${stage_version_stamp:-$(echo ${stage} | sed -E 's/.*stage[0-9]+-(.*)/\1-@TIMESTAMP@/; t; s/.*/@TIMESTAMP@/')}
					local _catalyst_conf=${stage_catalyst_conf:-${release_catalyst_conf:-${platform_catalyst_conf}}} # Can be added in platform, release or stage
					local _product=${stage_rel_type:-${_platform}/${_release}}/${_target}-${_subarch}-${_version_stamp}
					local _available_build=$(sanitize_spec_variable ${_platform} ${_release} ${_stage} ${_family} ${_basearch} ${_subarch} ${_product} | sed 's/@TIMESTAMP@/[0-9]{8}T[0-9]{6}Z/') && _available_build=$(printf "%s\n" "${available_builds[@]}" | grep -E ${_available_build} | sort -r | head -n 1 | sed 's/\.tar\.xz$//')
					local _is_selected=$(is_stage_selected ${_platform} ${_release} ${_stage})

					# Store determined variables and sanitize selected:
					stages[${stages_count},kind]=${_kind}
					stages[${stages_count},platform]=${_platform}
					stages[${stages_count},release]=${_release}
					stages[${stages_count},stage]=${_stage}
					stages[${stages_count},selected]=${_is_selected}
					stages[${stages_count},target]=${_target}
					stages[${stages_count},version_stamp]=$(sanitize_spec_variable ${_platform} ${_release} ${_stage} ${_family} ${_basearch} ${_subarch} ${_version_stamp})
					stages[${stages_count},source_subpath]=$(sanitize_spec_variable ${_platform} ${_release} ${_stage} ${_family} ${_basearch} ${_subarch} ${_source_subpath})
					stages[${stages_count},product]=$(sanitize_spec_variable ${_platform} ${_release} ${_stage} ${_family} ${_basearch} ${_subarch} ${_product})
					stages[${stages_count},overlays]=${_repos}
					stages[${stages_count},releng_base]=${_releng_base}
					stages[${stages_count},arch_basearch]=${_basearch}
					stages[${stages_count},arch_baseraw]=${_baseraw}
					stages[${stages_count},arch_subarch]=${_subarch}
					stages[${stages_count},arch_family]=${_family}
					stages[${stages_count},arch_interpreter]=${_interpreter}
					stages[${stages_count},chost]=${_chost}
					stages[${stages_count},common_flags]=${_common_flags}
					stages[${stages_count},cpu_flags]=${_cpu_flags}
					stages[${stages_count},compression_mode]=${_compression_mode}
					stages[${stages_count},available_build]=${_available_build}
					stages[${stages_count},catalyst_conf]=${_catalyst_conf}

					stages_count=$((stages_count + 1))

				fi
			done
		done
	done

	# Find initial parents here and use this indexes later to calculate other parameters
	update_parent_indexes

	# Generate virtual stages to download seeds for stages without local parents.
	declare -A added_remote_stages=()
	local i; for (( i=0; i<${stages_count}; i++ )); do
		# Skip for builds other than local and binhost
		if [[ ${stages[${i},kind]} != local ]] && [[ ${stages[${i},kind]} != binhost ]]; then
			continue
		fi
		# Skip if found local parent stage.
		if [[ -n ${stages[${i},parent]} ]]; then
			continue
		fi
		local seed_subpath=${stages[${i},source_subpath]}
		if ! contains_string added_remote_stages[@] ${seed_subpath}; then
			added_remote_stages[${seed_subpath}]=${stages_count}
			stages[${stages_count},kind]=remote
			stages[${stages_count},product]=${seed_subpath}
			stages[${stages_count},selected]=$([[ ${#selected_stages_templates[@]} -eq 0 ]] && echo true || echo false) # TODO: Allow to select somehow specific remote virtual builds too.
			stages[${stages_count},stage]=$(echo ${stages[${stages_count},product]} | awk -F '/' '{print $NF}' | sed 's/-@TIMESTAMP@//') # In virtual remotes, stage is determined this way
			# Find available build
			local _available_build=$(echo ${seed_subpath} | sed 's/@TIMESTAMP@/[0-9]{8}T[0-9]{6}Z/') && _available_build=$(printf "%s\n" "${available_builds[@]}" | grep -E "${_available_build}" | sort -r | head -n 1 | sed 's/\.tar\.xz$//')
			stages[${stages_count},available_build]=${_available_build}
			# Save interpreter and basearch as the same as the child that this new seed produces. In theory this could result in 2 or more stages using the same source while haveing different base architecture, but this should not be the case in properly configured templates.
			stages[${stages_count},arch_interpreter]=${stages[${i},arch_interpreter]}
			stages[${stages_count},arch_basearch]=${stages[${i},arch_basearch]}
			stages[${stages_count},arch_baseraw]=${stages[${i},arch_baseraw]}
			stages[${stages_count},arch_family]=${stages[${i},arch_family]}
			# Generate seed information download URL
			local seeds_arch_url=$(echo ${seeds_url} | sed "s/@ARCH_FAMILY@/${stages[${stages_count},arch_family]}/")
			stages[${stages_count},url]=${seeds_arch_url}/latest-${stages[${stages_count},stage]}.txt

			((stages_count++))
		fi
		stages[${i},parent]=${added_remote_stages[${seed_subpath}]}
	done
	unset added_remote_stages

	# Sort stages array by inheritance:
	declare stages_order=() # Order in which stages should be build, for inheritance to work. (1,5,2,0,...).
	# Prepare stages order by inheritance.
	local i; for (( i=0; i<${stages_count}; i++ )); do
		insert_stage_with_inheritance ${i}
	done
	# Sort stages by inheritance order in temp array..
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
			if [[ -n ${parent_index} ]] && ( [[ -z ${stages[${parent_index},available_build]} ]] || [[ ${CLEAN_BUILD} = true ]] ); then
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

	# Debug mode
	if [[ ${DEBUG} = true ]]; then
		for ((i=0; i<${stages_count}; i++)); do
			echo "Stage details at index ${i}:"
			for key in ${STAGE_KEYS[@]}; do
				printf "%-20s%s\n" "${key}:" "${stages[$i,$key]}"
			done
			echo "--------------------------------------------------------------------------------"
		done
	fi

	# List stages to build
	echo_color ${color_turquoise_bold} "[ Stages taking part in this process ]"
	draw_stages_tree
	echo ""
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

# Setup additional information for stages:
# Final download URL's.
# Real seed names, with timestamp replaced.
# Final paths for remote repositories.
prepare_stages() {
	echo_color ${color_turquoise_bold} "[ Preparing stages ]"

	local i; for (( i=0; i<${stages_count}; i++ )); do
		# Prepare only stages that needs rebuild.
		if [[ ${stages[${i},rebuild]} = false ]]; then
			continue
		fi

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

		# Replace spec templates with real data:
		echo "" >> ${stage_spec_work_path} # Add new line, to separate new entries
		echo "# Added by catalyst-lab" >> ${stage_spec_work_path}

		# Add target prefix to things like use, rcadd, unmerge, etc.
		local target_keys=(use packages unmerge rcadd rcdel rm empty iso volid fstype gk_mainargs type fsscript groups root_overlay ssh_public_keys users linuxrc bootargs cdtar depclean fsops modblacklist motd overlay readme verify)
		for target_key in ${target_keys[@]}; do
			sed -i "s|^${target_key}:|${target_mapping}/${target_key}:|" ${stage_spec_work_path}
		done

		set_spec_variable_if_missing ${stage_spec_work_path} target ${target}
		set_spec_variable_if_missing ${stage_spec_work_path} rel_type ${platform}/${release}
		set_spec_variable_if_missing ${stage_spec_work_path} subarch ${arch_subarch}
		set_spec_variable_if_missing ${stage_spec_work_path} version_stamp ${version_stamp}
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
		if contains_string COMPRESSABLE_TARGETS[@] ${target}; then
			set_spec_variable_if_missing ${stage_spec_work_path} compression_mode ${compression_mode:-pixz} # If not specified in platform/release, use pixz as default value
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

		# Special variables for only some stages.
		if [[ ${target} = stage1 ]]; then
			set_spec_variable_if_missing ${stage_spec_work_path} update_seed yes
			set_spec_variable_if_missing ${stage_spec_work_path} update_seed_command "--changed-use --update --deep --usepkg --buildpkg @system @world"
		fi
		if [[ ${target} = stage4 ]]; then
			set_spec_variable_if_missing ${stage_spec_work_path} binrepo_path ${platform}/${release}
		fi
		# LiveCD - stage2 specific default values.
		if [[ ${target} = livecd-stage2 ]]; then
			set_spec_variable_if_missing ${stage_spec_work_path} type gentoo-release-minimal
			set_spec_variable_if_missing ${stage_spec_work_path} volid Gentoo_${platform}
			set_spec_variable_if_missing ${stage_spec_work_path} fstype squashfs
			set_spec_variable_if_missing ${stage_spec_work_path} iso install-${platform}-${timestamp}.iso
		fi

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

		echo_color ${color_turquoise} "Building stage: ${platform}/${release}/${stage}"
		echo ""
		local args="-af ${stage_spec_work_path}"
		if [[ -n ${catalyst_conf} ]]; then
			args="${args} -c ${catalyst_conf}"
		fi
		catalyst $args || exit 1

		echo_color ${color_green} "Stage build completed: ${platform}/${release}/${stage}"
		echo ""
	done
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

	local idx=${1}
	local prefix=${2}

	# Reset previous values:
	for variable in ${STAGE_KEYS[@]}; do
		unset ${prefix}${variable}
		unset parent_${prefix}${variable}
	done

	if [[ ${stages[${idx},kind]} = local ]]; then
		# Handle loading details of local stage job

		# Automatically determine all possible keys stored in stages, and load them to variables.
		#local keys=($(printf "%s\n" "${!stages[@]}" | sed 's/.*,//' | sort -u))
		for variable in ${STAGE_KEYS[@]}; do
			local value=${stages[${idx},${variable}]}
			eval "${prefix}${variable}='${value}'"
		done

		# Load platform config
		if [[ -z ${prefix} ]]; then
			# TODO: Store these in stage instead of loading manually
			# Platform config
			# If some properties are not set in config - unset them while loading new config
			unset repos common_flags chost cpu_flags compression_mode
			local platform_conf_path=${templates_path}/${platform}/platform.conf
			source ${platform_conf_path}
			local release_conf_path=${templates_path}/${platform}/${release}/release.conf
			if [[ -f ${release_conf_path} ]]; then
				# Variables in release.conf can overwrite platform defaults.
				source ${release_conf_path}
			fi
		fi

        elif [[ ${stages[${idx},kind]} = remote ]]; then
		# Handle loading of remote task details
		kind=${stages[${idx},kind]}
		url=${stages[${idx},url]}
		rebuild=${stages[${idx},rebuild]}
		# ...
	fi

	# Load also parent info for supported targets
	if [[ -z ${prefix} ]] && ( [[ ${kind} = local ]] || [[ ${kind} = binhost ]] ); then
		if [[ -n ${parent} ]]; then
			use_stage ${parent} parent_
		fi
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

# Scans local and binhost targets and updates their parent property in stages array.
update_parent_indexes() {
	local i; for (( i=0; i<${stages_count}; i++ )); do
		# Search for parents only for supported stages
                if [[ ${stages[${i},kind]} != local ]] && [[ ${stages[${i},kind]} != binhost ]]; then
                        continue
                fi
		stages[${i},parent]='' # Reset previously set parent index if any.
		local j; for (( j=0; j<${stages_count}; j++ )); do
			if [[ ${stages[${i},source_subpath]} == ${stages[${j},product]} ]]; then
				# Save found parent index
				stages[${i},parent]=${j}
				break
			fi
		done
	done
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
		if [[ -n ${parent} ]]; then

			# Check for cicrular dependencies
			if [[ ${dependency_stack} == *"|${parent}|"* ]]; then
				dependency_stack="${dependency_stack}${index}|"
				echo "Circular dependency detected for ${parent_platform}/${parent_release}/${parent_stage}. Verify your templates."
				IFS='|' read -r -a dependency_indexes <<< "${dependency_stack#|}"
				echo "Stack:"
				local found_parent=false
				for i in ${dependency_indexes[@]}; do
					if [[ ${found_parent} = false ]] && [[ ${parent} != ${i} ]]; then
						continue
					fi
					found_parent=true
					echo ${stages[${i},platform]}/${stages[${i},release]}/${stages[${i},stage]}
				done
				exit 1
			fi

			# Insert parent before current index
			local next_dependency_stack="${dependency_stack}${index}|"
			insert_stage_with_inheritance ${parent} "${next_dependency_stack}"
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
		local stage_name="?"
		if [[ ${stages[${child},kind]} = local ]]; then
			local display_name=${stages[${child},platform]}/${stages[${child},release]}/${stages[${child},stage]}
			stage_name=${color_gray}${display_name}${color_nc}
			# If stage is not being rebuild and it has direct children that are being rebuild, display used available_build.
			if [[ ${stages[${child},rebuild]} == false ]] && [[ -n ${stages[${child},available_build]} ]]; then
				for c in ${stages[${child},children]}; do
					if [[ ${stages[${c},rebuild]} = true ]]; then
						display_name="${display_name} (${stages[${child},available_build]})"
						stage_name=${color_gray}${display_name}${color_nc}
						break
					fi
				done
			fi
			if [[ ${stages[${child},rebuild]} = true ]]; then
				stage_name=${stages[${child},platform]}/${stages[${child},release]}/${color_turquoise}${stages[${child},stage]}${color_nc}
			fi
			if [[ ${stages[${child},selected]} = true ]]; then
				stage_name=${stages[${child},platform]}/${stages[${child},release]}/${color_turquoise_bold}${stages[${child},stage]}${color_nc}
			fi
		elif [[ ${stages[${child},kind]} = remote ]]; then
			local display_name=${stages[${child},product]}
			# If stage is not being rebuild and it has direct children that are being rebuild, display used available_build.
			if [[ ${stages[${child},rebuild]} == false ]] && [[ -n ${stages[${child},available_build]} ]]; then
				for c in ${stages[${child},children]}; do
					if [[ ${stages[${c},rebuild]} = true ]]; then
						display_name="${display_name} (${stages[${child},available_build]})"
						break
					fi
				done
			fi
			stage_name="${color_gray}remote: ${display_name}${color_nc}"
			if [[ ${stages[${child},rebuild]} = true ]]; then
				stage_name="remote: ${color_yellow}${display_name}${color_nc}"
			fi
			if [[ ${stages[${child},selected]} = true ]]; then
				stage_name="remote: ${color_yellow_bold}${display_name}${color_nc}"
			fi
		elif [[ ${stages[${child},kind]} = binhost ]]; then
			stage_name="[ Binhost update ]" # TODO: Better display of this kind of stages
		fi
		new_prefix="${prefix}├── "
		if [[ -n ${stages[${child},children]} ]]; then
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

# ------------------------------------------------------------------------------
# START:

# Initial config

# Check for root privilages.
if [[ $EUID -ne 0 ]]; then
	echo "This script must be run as root"
	exit 1
fi

declare PLATFORM_KEYS=( # Variables allowed in platform.conf
	arch
	repos
	common_flags
	chost
	cpu_flags
	compression_mode
)
declare RELEASE_KEYS=( # Variables allowed in release.conf
	repos
	common_flags
	chost
	cpu_flags
	compression_mode
)
declare STAGE_KEYS=( # Variables stored in stages[]
	kind

	arch_basearch
	arch_baseraw
	arch_subarch
	arch_family
	arch_interpreter

	platform
	release
	stage

	target
	product
	available_build
	source_subpath
	parent
	children

	chost
	common_flags
	cpu_flags

	treeish
	overlays
	catalyst_conf
	releng_base
	version_stamp
	compression_mode

	selected		# Is explicitly selected by the user or no selection was made.
	rebuild			# Will be rebuild in this run.
	takes_part		# Is in branch where anything get's rebuild - either itself or it's children.

	url
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
	[stage1]=stages
	[stage2]=stages
	[stage3]=stages
	[stage4]=stages
	[livecd-stage1]=isos
	[livecd-stage2]=isos
)
# List of targets that are compressed after build. This allows adding compression_mode property automatically to stages.
declare COMPRESSABLE_TARGETS=(stage1 stage2 stage3 stage4 livecd-stage1 livecd-stage2)

readonly host_arch=${ARCH_MAPPINGS[$(arch)]:-$(arch)} # Mapped to release arch
readonly timestamp=$(date -u +"%Y%m%dT%H%M%SZ") # Current timestamp.
readonly qemu_has_static_user=$(grep -q static-user /var/db/pkg/app-emulation/qemu-*/USE && echo true || echo false)
readonly qemu_binfmt_is_running=$( { [ -x /etc/init.d/qemu-binfmt ] && /etc/init.d/qemu-binfmt status | grep -q started; } || { pidof systemd >/dev/null && systemctl is-active --quiet qemu-binfmt; } && echo true || echo false )

readonly color_gray='\033[0;90m'
readonly color_red='\033[0;31m'
readonly color_green='\033[0;32m'
readonly color_turquoise='\033[0;36m'
readonly color_turquoise_bold='\033[1;36m'
readonly color_yellow='\033[0;33m'
readonly color_yellow_bold='\033[1;33m'
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
	--debug) DEBUG=true;;
	--*) echo "Unknown option ${1}"; exit;;
	-*) echo "Unknown option ${1}"; exit;;
	*) selected_stages_templates+=("${1}");;
esac; shift; done

# ------------------------------------------------------------------------------
# Main program:

load_stages
if [[ ${PREPARE} = true ]]; then
	prepare_portage_snapshot
	prepare_releng
	prepare_stages
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
# TODO: Add checking for valid config entries in config files
# TODO: Detect when profile changes in stage4 and if it does, automtically add rebuilds to fsscript file
# TODO: Introduce stage types. Download (added automatially for downloding missing seeds), Build (standard build stage), Binhost (building additional packages)
# TODO: Define parent property for setting source_subpath. Parent can be name of stage, full name of stage (including platform and release) or remote. With remote if can just specify word remote and automatically find like, it it can specify tarball name or even full URL.
