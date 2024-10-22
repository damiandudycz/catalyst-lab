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
					local stage_binrepo=$(read_spec_variable ${stage_spec_path} binrepo)
					local stage_binrepo_path=$(read_spec_variable ${stage_spec_path} binrepo_path)
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
					local _emulation=$( [[ ${host_arch} = ${_basearch} ]] && echo false || echo true )
					local _subarch=${stage_subarch:-${platform_subarch}} # Can be skipped in spec, will be determined from platform.conf
					local _releng_base=${stage_releng_base:-${RELENG_BASES[${_target}]}} # Can be skipped in spec, will be determined automatically from target
					local _source_subpath=${stage_source_subpath}
					local _treeish=${stage_treeish} # Can be skipped in spec, will use newest seed available
					local _repos=${stage_repos:-${release_repos:-${platform_repos}}} # Can be definied in platform, release or stage (spec)
					local _rel_type=${stage_rel_type:-${platform}/${release}}

					local _binrepo=${stage_binrepo:-${release_binrepo:-${platform_binrepo:-${binpkgs_cache_path}/local}}}
					local _binrepo_path=${stage_binrepo_path:-${release_binrepo_path:-${platform_binrepo_path:-${_rel_type}}}}

					local _chost=${stage_chost:-${release_chost:-${platform_chost}}} # Can be definied in platform, release or stage (spec)
					local _cpu_flags=${stage_cpu_flags:-${release_cpu_flags:-${platform_cpu_flags}}} # Can be definied in platform, release or stage (spec)
					local _common_flags=${stage_common_flags:-${release_common_flags:-${platform_common_flags}}} # Can be definied in platform, release or stage (spec)
					local _compression_mode=${stage_compression_mode:-${release_compression_mode:-${platform_compression_mode:-pixz}}} # Can be definied in platform, release or stage (spec)
					local _version_stamp=${stage_version_stamp:-$(echo ${stage} | sed -E 's/.*stage[0-9]+-(.*)/\1-@TIMESTAMP@/; t; s/.*/@TIMESTAMP@/')}
					local _catalyst_conf=${stage_catalyst_conf:-${release_catalyst_conf:-${platform_catalyst_conf}}} # Can be added in platform, release or stage
					local _product=${stage_rel_type:-${_platform}/${_release}}/${_target}-${_subarch}-${_version_stamp}
					local _available_build=$(sanitize_spec_variable ${_platform} ${_release} ${_stage} ${_family} ${_basearch} ${_subarch} ${_product} | sed 's/@TIMESTAMP@/[0-9]{8}T[0-9]{6}Z/') && _available_build=$(printf "%s\n" "${available_builds[@]}" | grep -E ${_available_build} | sort -r | head -n 1 | sed 's/\.tar\.xz$//')
					local _available_build_timestamp=$( [[ -n ${_available_build} ]] && start_pos=$(expr index "${_product}" "@TIMESTAMP@") && echo "${_available_build:$((start_pos - 1)):16}" )
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
					stages[${stages_count},binrepo]=$(sanitize_spec_variable ${_platform} ${_release} ${_stage} ${_family} ${_basearch} ${_subarch} ${_binrepo})
					stages[${stages_count},binrepo_path]=$(sanitize_spec_variable ${_platform} ${_release} ${_stage} ${_family} ${_basearch} ${_subarch} ${_binrepo_path})
					stages[${stages_count},rel_type]=$(sanitize_spec_variable ${_platform} ${_release} ${_stage} ${_family} ${_basearch} ${_subarch} ${_rel_type})
					stages[${stages_count},releng_base]=${_releng_base}
					stages[${stages_count},arch_basearch]=${_basearch}
					stages[${stages_count},arch_baseraw]=${_baseraw}
					stages[${stages_count},arch_subarch]=${_subarch}
					stages[${stages_count},arch_family]=${_family}
					stages[${stages_count},arch_interpreter]=${_interpreter}
					stages[${stages_count},arch_emulation]=${_emulation}
					stages[${stages_count},chost]=${_chost}
					stages[${stages_count},common_flags]=${_common_flags}
					stages[${stages_count},cpu_flags]=${_cpu_flags}
					stages[${stages_count},compression_mode]=${_compression_mode}
					stages[${stages_count},available_build]=${_available_build}
					stages[${stages_count},timestamp_available]=${_available_build_timestamp}
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
		[[ ${stages[${i},kind]} != local ]] && [[ ${stages[${i},kind]} != binhost ]] && continue
		# Skip if found local parent stage.
		[[ -n ${stages[${i},parent]} ]] && continue

		local seed_subpath=${stages[${i},source_subpath]}
		if ! contains_string added_remote_stages[@] ${seed_subpath}; then
			added_remote_stages[${seed_subpath}]=${stages_count}
			stages[${stages_count},kind]=remote
			stages[${stages_count},product]=${seed_subpath}
			stages[${stages_count},platform]=${stages[${i},arch_family]} # Use arch family as platform for virtual remote jobs
			stages[${stages_count},release]=gentoo # Use constant name gentoo for virtual remote stages
			stages[${stages_count},stage]=$(echo ${stages[${stages_count},product]} | awk -F '/' '{print $NF}' | sed 's/-@TIMESTAMP@//') # In virtual remotes, stage is determined this way
			stages[${stages_count},target]=$(echo ${stages[${stages_count},stage]} | sed -E 's/(.*stage[0-9]+)-.*/\1/')
			local _is_selected=$(is_stage_selected ${stages[${stages_count},platform]} ${stages[${stages_count},release]} ${stages[${stages_count},stage]})
			stages[${stages_count},selected]=$(is_stage_selected ${stages[${stages_count},platform]} ${stages[${stages_count},release]} ${stages[${stages_count},stage]})
			# Find available build
			local _available_build=$(echo ${seed_subpath} | sed 's/@TIMESTAMP@/[0-9]{8}T[0-9]{6}Z/') && _available_build=$(printf "%s\n" "${available_builds[@]}" | grep -E "${_available_build}" | sort -r | head -n 1 | sed 's/\.tar\.xz$//')
			local _available_build_timestamp=$( [[ -n ${_available_build} ]] && start_pos=$(expr index "${seed_subpath}" "@TIMESTAMP@") && echo "${_available_build:$((start_pos - 1)):16}" )
			stages[${stages_count},available_build]=${_available_build}
			stages[${stages_count},timestamp_available]=${_available_build_timestamp}
			# Save interpreter and basearch as the same as the child that this new seed produces. In theory this could result in 2 or more stages using the same source while haveing different base architecture, but this should not be the case in properly configured templates.
			stages[${stages_count},arch_interpreter]=${stages[${i},arch_interpreter]}
			stages[${stages_count},arch_basearch]=${stages[${i},arch_basearch]}
			stages[${stages_count},arch_baseraw]=${stages[${i},arch_baseraw]}
			stages[${stages_count},arch_family]=${stages[${i},arch_family]}
			# Emulation mode. For remote stages, it's not gonna be used, but it's still determined in case some other functionalities uses this stages directly.
			local _emulation=$( [[ ${host_arch} = ${stages[${stages_count},arch_basearch]} ]] && echo false || echo true )
			stages[${stages_count},arch_emulation]=${_emulation}
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

	# List stages to build
	echo_color ${color_turquoise_bold} "[ Stages taking part in this process ]"
	draw_stages_tree
	echo ""

	# Determinel overlays local paths.
	local i; for (( i=0; i<${stages_count}; i++ )); do
		if [[ -n ${stages[${i},overlays]} ]]; then
			# Map remote repositories to local names
			local local_repo_urls=()
			for repo in ${stages[${i},overlays]}; do
				local_repo_urls+=$(repo_local_path ${repo})
			done
			local_repo_urls="${local_repo_urls[@]}" # Map to string
			stages[${i},overlays_local_paths]="${local_repo_urls}" # Save local paths in stage details.
		fi
	done

	# Determine binrepos local paths and types.
	local i; for (( i=0; i<${stages_count}; i++ )); do
		if [[ -n ${stages[${i},binrepo]} ]]; then
			if [[ ${stages[${i},binrepo]} == http://* || ${stages[${i},binrepo]} == https://* || ${stages[${i},binrepo]} == git@* ]]; then
				stages[${i},binrepo_kind]=git
			else
				stages[${i},binrepo_kind]=local
			fi
			# Map remote repositories to local names
			stages[${i},binrepo_local_path]=$(binrepo_local_path ${stages[${i},binrepo]})
		else
			stages[${i},binrepo_local_path]=${stages[${i},binrepo]}
		fi
	done

	# Determine timestamp_generated property - only for local for now.
	# For remote this is filled after checking available remote build.
	for ((i=$((stages_count - 1)); i>=0; i--)); do
		( [[ ${stages[${i},rebuild]} = false ]] || [[ ${stages[${i},kind]} = remote ]] ) && continue
		stages[${i},timestamp_generated]=${timestamp}
	done

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

fetch_repos() {
	echo_color ${color_turquoise_bold} "[ Preparing remote repositories ]"

	# Process remote overlay repos.
	local handled_repos=()
	local i; for (( i=0; i<${stages_count}; i++ )); do
		if [[ -n ${stages[${i},overlays]} ]]; then
			# Clone/pull used repositories
			for repo in ${stages[${i},overlays]}; do
				[[ ${stages[${i},rebuild]} = true ]] || continue # Only fetch for selected repos
				contains_string handled_repos[@] ${repo} && continue
				handled_repos+=(${repo})
				# Check if is remote repository
				if [[ ${repo} == http://* || ${repo} == https://* ]]; then
					local repo_local_path=$(repo_local_path ${repo})
					if [[ ! -d ${repo_local_path} ]]; then
						# If location doesn't exists yet - clone repository
						echo -e "${color_turquoise}Clonning overlay repo: ${color_yellow}${repo}${color_nc}"
						mkdir -p ${repo_local_path}
						git clone ${repo} ${repo_local_path}
					elif [[ ${FETCH_FRESH_REPOS} = true ]]; then
						# If it exists - pull repository
						echo -e "${color_turquoise}Pulling overlay repo: ${color_yellow}${repo}${color_nc}"
						git -C ${repo_local_path} pull
					fi
				fi
			done
		fi
	done
	unset handled_repos

	# Process remote binrepos.
	local handled_repos=()
	local i; for (( i=0; i<${stages_count}; i++ )); do
		if [[ -n ${stages[${i},binrepo]} ]]; then
			[[ ${stages[${i},rebuild]} = true ]] || continue # Only fetch for building repos
			# Clone/pull used repositories
			if ! contains_string handled_repos[@] ${stages[${i},binrepo]}; then
				handled_repos+=(${stages[${i},binrepo]})
				# Check if is remote repository
				if [[ ${stages[${i},binrepo_kind]} == git ]]; then
					local repo_local_path=$(binrepo_local_path ${stages[${i},binrepo]})
					if [[ ! -f ${stages[${i},binrepo_local_path]}/.git ]]; then
						# If location doesn't exists yet - clone repository
						echo -e "${color_turquoise}Clonning binrepo: ${color_yellow}${stages[${i},binrepo]}${color_nc}"
						mkdir -p ${repo_local_path}
						git clone ${stages[${i},binrepo]} ${stages[${i},binrepo_local_path]}
					elif [[ ${FETCH_FRESH_REPOS} = true ]]; then
						# If it exists - pull repository
						echo -e "${color_turquoise}Pulling binrepo: ${color_yellow}${stages[${i},binrepo]}${color_nc}"
						git -C ${stages[${i},binrepo_local_path]} pull
					fi
				fi
			fi
		fi
	done
	echo ""
}

# Setup additional information for stages:
# Final download URL's.
# Real seed names, with timestamp replaced.
# Refresh remote repos.
prepare_stages() {
	echo_color ${color_turquoise_bold} "[ Preparing stages ]"

	local i; for (( i=0; i<${stages_count}; i++ )); do
		# Prepare only stages that needs rebuild.
		if [[ ${stages[${i},rebuild]} = true ]]; then
			# Update treeish property for local builds only.
			[[ ${stages[${i},kind]} = local ]] && stages[${i},treeish]=${stages[${i},treeish]:-${treeish}}

			# Prepare remote builds newest timestamp and url from backend.
			if [[ ${stages[${i},kind]} = remote ]]; then
				echo -e "${color_turquoise}Getting seed info: ${color_yellow}${stages[${i},platform]}/${stages[${i},release]}/${stages[${i},stage]}${color_nc}"
				local metadata_content=$(wget -q -O - ${stages[${i},url]} --no-http-keep-alive --no-cache --no-cookies)
				local stage_regex=${stages[${i},stage]}"-[0-9]{8}T[0-9]{6}Z"
				local latest_seed=$(echo "${metadata_content}" | grep -E ${stage_regex} | head -n 1 | cut -d ' ' -f 1)
				local arch_url=$(echo ${seeds_url} | sed "s/@ARCH_FAMILY@/${stages[${i},arch_family]}/")
				# Replace URL from metadata url to stage download url
				stages[${i},url]=${arch_url}/${latest_seed}
				# Extract remote available timestamp and store it in stage timestamp_generated
				stages[${i},timestamp_generated]=$(echo ${latest_seed} | sed -n -r "s|.*${stages[${i},stage]}-([0-9]{8}T[0-9]{6}Z).*|\1|p")
			fi
		fi

		# Fill timestamps in stages.
		local stage_timestamp=${stages[${i},timestamp_generated]:-${stages[${i},timestamp_available]}}
		if [[ -n ${stage_timestamp} ]]; then
			# Update stage_timestamp in product and version_stamp of this target
			stages[${i},product]=$(echo ${stages[${i},product]} | sed "s|@TIMESTAMP@|${stage_timestamp}|")
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

	local i; local j=0; for (( i=0; i<${stages_count}; i++ )); do
		[[ ${stages[${i},rebuild]} = false ]] && continue
		((j++))

		# Create stages final files - spec, catalyst.conf, portage_confdir, root_overlay, overlay, fstype and download job scripts.
		# Treat remote and local jobs differently here.

		local platform_path=${templates_path}/${stages[${i},platform]}
		local platform_path_work=${work_path}/${stages[${i},platform]}
		local release_path=${platform_path}/${stages[${i},release]}
		local release_path_work=${platform_path_work}/${stages[${i},release]}
		local stage_path=${release_path}/${stages[${i},stage]}
		local stage_path_work=${release_path_work}/${stages[${i},stage]}
		local spec_link_work=$(printf ${work_path}/jobs/%03d.${stages[${i},platform]}-${stages[${i},release]}-${stages[${i},stage]} ${j})

		# Copy stage template workfiles to work_path. For virtual stages, just create work directory (virtual stages don't have existing stage_path directory).
		mkdir -p ${stage_path_work}
		if [[ -d ${stage_path} ]]; then
			cp -rf ${stage_path}/* ${stage_path_work}/
		fi

		if [[ ${stages[${i},kind]} = local ]]; then

			# Setup used paths:
			local portage_path_work=${stage_path_work}/portage
			local catalyst_conf_work=${stage_path_work}/catalyst.conf
			local stage_overlay_path_work=${stage_path_work}/overlay
			local stage_root_overlay_path_work=${stage_path_work}/root_overlay
			local stage_fsscript_path_work=${stage_path_work}/fsscript.sh
			local stage_spec_path_work=${stage_path_work}/stage.spec
			local target_mapping=${TARGET_MAPPINGS[${stages[${i},target]}]:-${stages[${i},target]}}
			local stage_pkgcache_path=${stages[${i},binrepo_local_path]}/${stages[${i},binrepo_path]}

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
			fi

			# Replace spec templates with real data:
			echo "" >> ${stage_spec_path_work} # Add new line, to separate new entries
			echo "# Added by catalyst-lab" >> ${stage_spec_path_work}

			set_spec_variable ${stage_spec_path_work} source_subpath ${stages[${i},source_subpath]} # source_subpath shoud always be replaced with calculated value, to take into consideration existing old builds usage.

			set_spec_variable_if_missing ${stage_spec_path_work} target ${stages[${i},target]}
			set_spec_variable_if_missing ${stage_spec_path_work} rel_type ${stages[${i},rel_type]}
			set_spec_variable_if_missing ${stage_spec_path_work} subarch ${stages[${i},arch_subarch]}
			set_spec_variable_if_missing ${stage_spec_path_work} version_stamp ${stages[${i},version_stamp]}
			set_spec_variable_if_missing ${stage_spec_path_work} snapshot_treeish ${stages[${i},treeish]}
			set_spec_variable_if_missing ${stage_spec_path_work} portage_confdir ${portage_path_work}
			set_spec_variable_if_missing ${stage_spec_path_work} pkgcache_path ${stage_pkgcache_path}

			update_spec_variable ${stage_spec_path_work} TIMESTAMP ${stages[${i},timestamp_generated]}
			update_spec_variable ${stage_spec_path_work} PLATFORM ${stages[${i},platform]}
			update_spec_variable ${stage_spec_path_work} RELEASE ${stages[${i},release]}
			update_spec_variable ${stage_spec_path_work} TREEISH ${stages[${i},treeish]}
			update_spec_variable ${stage_spec_path_work} FAMILY_ARCH ${stages[${i},arch_family]}
			update_spec_variable ${stage_spec_path_work} BASE_ARCH ${stages[${i},arch_basearch]}
			update_spec_variable ${stage_spec_path_work} SUB_ARCH ${stages[${i},arch_subarch]}
			update_spec_variable ${stage_spec_path_work} PKGCACHE_BASE_PATH ${pkgcache_base_path}

			# releng portage_prefix.
			if [[ -n ${stages[${i},releng_base]} ]]; then
				set_spec_variable_if_missing ${stage_spec_path_work} portage_prefix releng
				sed -i '/^releng_base:/d' ${stage_spec_path_work}
			fi

			[[ -n ${stages[$${i},common_flags]} ]] && set_spec_variable_if_missing ${stage_spec_path_work} common_flags "${stages[${i},common_flags]}"
			[[ ${stages[${i},arch_emulation]} = true ]] && set_spec_variable_if_missing ${stage_spec_path_work} interpreter "${stages[${i},arch_interpreter]}"
			[[ -d ${stage_overlay_path_work} ]] && set_spec_variable_if_missing ${stage_spec_path_work} ${target_mapping}/overlay ${stage_overlay_path_work}
			[[ -d ${stage_root_overlay_path_work} ]] && set_spec_variable_if_missing ${stage_spec_path_work} ${target_mapping}/root_overlay ${stage_root_overlay_path_work}
			[[ -f ${stage_fsscript_path_work} ]] && set_spec_variable_if_missing ${stage_spec_path_work} ${target_mapping}/fsscript ${stage_fsscript_path_work}
			[[ -n ${stages[${i},overlays_local_paths]} ]] && set_spec_variable_if_missing ${stage_spec_path_work} repos "${stages[${i},overlays_local_paths]}"

			# Special variables for only some stages:

			# Update seed.
			if [[ ${stages[${i},target]} = stage1 ]]; then
				set_spec_variable_if_missing ${stage_spec_path_work} update_seed yes
				set_spec_variable_if_missing ${stage_spec_path_work} update_seed_command "--changed-use --update --deep --usepkg --buildpkg @system @world"
			fi

			# Binrepo path.
			if [[ ${stages[${i},target]} = stage4 ]]; then
				set_spec_variable_if_missing ${stage_spec_path_work} binrepo_path ${stages[${i},binrepo_path]}
			fi

			# LiveCD - stage2 specific default values.
			if [[ ${stages[${i},target]} = livecd-stage2 ]]; then
				set_spec_variable_if_missing ${stage_spec_path_work} type gentoo-release-minimal
				set_spec_variable_if_missing ${stage_spec_path_work} volid Gentoo_${stages[${i},platform]}
				set_spec_variable_if_missing ${stage_spec_path_work} fstype squashfs
				set_spec_variable_if_missing ${stage_spec_path_work} iso install-${stages[${i},platform]}-${stages[${i},timestamp]}.iso
			fi

			if [[ -n ${stages[${i},chost]} ]] && [[ ${stages[${i},target]} = stage1 ]]; then # Only allow setting chost in stage1 targets.
				set_spec_variable_if_missing ${stage_spec_path_work} chost ${stages[${i},chost]}
			fi

			if contains_string COMPRESSABLE_TARGETS[@] ${stages[${i},target]}; then
				set_spec_variable_if_missing ${stage_spec_path_work} compression_mode ${stages[${i},compression_mode]}
			fi

			# Add target prefix to things like use, rcadd, unmerge, etc.
			for target_key in ${TARGET_KEYS[@]}; do
				sed -i "s|^${target_key}:|${target_mapping}/${target_key}:|" ${stage_spec_path_work}
			done

			# Create links to spec files and optionally to catalyst_conf if using custom.
			ln -s ${stage_spec_path_work} ${spec_link_work}.spec
			[[ -f ${catalyst_conf_work} ]] && ln -s ${catalyst_conf_work} ${spec_link_work}.catalyst.conf

		elif [[ ${stages[${i},kind]} = remote ]]; then
			# Create stage for remote download:

			local download_script_path_work=${stage_path_work}/download.sh
			local download_path=${catalyst_builds_path}/${stages[${i},product]}.tar.xz
			local download_dir=$(dirname ${download_path})

			# Prepare download script for remote job.
			echo "#!/bin/bash" > ${download_script_path_work}
			echo "file=${download_path}" >> ${download_script_path_work}
			echo "[[ -f \${file} ]] && echo 'File already exists' && exit" >> ${download_script_path_work} # Skip download if file already exists
			echo "mkdir -p ${download_dir}" >> ${download_script_path_work}
			echo "trap '[[ -f \${file} ]] && rm -f \${file}' EXIT INT" >> ${download_script_path_work}
			echo "wget ${stages[${i},url]} -O \${file} || exit 1" >> ${download_script_path_work}
			echo "trap - EXIT" >> ${download_script_path_work}
			chmod +x ${download_script_path_work}

			# Create link to download script.
			ln -s ${download_script_path_work} ${spec_link_work}.sh
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

		if [[ ${stages[${i},kind]} = local ]]; then
			echo -e "${color_turquoise}Building stage: ${color_turquoise}${stages[${i},platform]}/${stages[${i},release]}/${stages[${i},stage]}${color_nc}"
			echo ""

			# Setup used paths:
			local stage_spec_path_work=${stage_path_work}/stage.spec
			local catalyst_conf_work=${stage_work_path}/catalyst.conf

			local args="-af ${stage_spec_path_work}"
			[[ -f ${catalyst_conf_work} ]] && args="${args} -c ${catalyst_conf_work}"

			# Perform build
			catalyst $args || exit 1

			echo -e "${color_green}Stage build completed: ${color_turquoise}${stages[${i},platform]}/${stages[${i},release]}/${stages[${i},stage]}${color_nc}"
			echo ""
		elif [[ ${stages[${i},kind]} = remote ]]; then
			echo -e "${color_turquoise}Downloading stage: ${color_yellow}${stages[${i},platform]}/${stages[${i},release]}/${stages[${i},stage]}${color_nc}"
			echo ""

			# Setup used paths:
			local download_script_path_work=${stage_path_work}/download.sh

			# Perform build
			${download_script_path_work} || exit 1

			echo -e "${color_green}Stage download completed: ${color_yellow}${stages[${i},platform]}/${stages[${i},release]}/${stages[${i},stage]}${color_nc}"
			echo ""
		fi

	done
	echo ""
}

upload_binrepos() {
	echo_color ${color_turquoise_bold} "[ Uploading binrepos ]"
	local handled_repos=()
	local i; for (( i=0; i<${stages_count}; i++ )); do
		[[ ${stages[${i},binrepo_kind]} = git ]] || continue
		[[ ${stages[${i},selected]} = true ]] || ( [[ ${stages[${i},rebuild]} = true ]] && [[ ${BUILD} = true ]] ) || continue # Only upload selected repos or rebild if building now
		[[ -f ${stages[${i},binrepo_local_path]}/.git ]] || continue # Skip if this repo doesnt yet exists
		contains_string handled_repos[@] ${stages[${i},binrepo]} && continue
		handled_repos+=(${stages[${i},binrepo]})
		echo -e "${color_turquoise}Uploading binrepo: ${color_yellow}${stages[${i},binrepo]}${color_nc}"
		# Check if there are changes to commit
		local changes=false
		if [[ -n $(git -C ${stages[${i},binrepo_local_path]} status --porcelain) ]]; then
			git -C ${stages[${i},binrepo_local_path]} add -A
			git -C ${stages[${i},binrepo_local_path]} commit -m "Automatic update: ${timestamp}"
			changes=true
		fi
		# Check if there are some commits to push
		if ! git -C "${stages[${i},binrepo_local_path]}" diff --exit-code origin/$(git -C "${stages[${i},binrepo_local_path]}" rev-parse --abbrev-ref HEAD) --quiet; then
			# Check for write access.
			if repo_url=$(git -C ${stages[${i},binrepo_local_path]} config --get remote.origin.url) && [[ ! "$repo_url" =~ ^https:// ]] && git -C ${stages[${i},binrepo_local_path]} ls-remote &>/dev/null && git -C ${stages[${i},binrepo_local_path]} push --dry-run &>/dev/null; then
				git -C ${stages[${i},binrepo_local_path]} push
			else
				echo -e "${color_yellow}Warning! No write access to binrepo: ${color_yellow}${stages[${i},binrepo]}${color_nc}"
			fi
			changes=true
		fi
		if [[ ${changes} = false ]]; then
			echo "No local changes detected"
		fi
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
	echo "${value}" | sed "s/@RELEASE@/${release}/g" | sed "s/@PLATFORM@/${platform}/g" | sed "s/@STAGE@/${stage}/g" | sed "s/@BASE_ARCH@/${base_arch}/g" | sed "s/@SUB_ARCH@/${sub_arch}/g" | sed "s/@FAMILY_ARCH@/${family}/g"
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
		local stage_name="?"
		if [[ ${stages[${child},kind]} = local ]]; then
			local display_name=${stages[${child},platform]}/${stages[${child},release]}/${stages[${child},stage]}
			stage_name=${color_gray}${display_name}${color_nc}
			# If stage is not being rebuild and it has direct children that are being rebuild, display used available_build.
			if [[ ${stages[${child},rebuild]} == false ]] && [[ -n ${stages[${child},timestamp_available]} ]]; then
				for c in ${stages[${child},children]}; do
					if [[ ${stages[${c},rebuild]} = true ]]; then
						display_name="${display_name} (${stages[${child},timestamp_available]})"
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
			local display_name=${stages[${child},platform]}/${stages[${child},release]}/${stages[${child},stage]}
			# If stage is not being rebuild and it has direct children that are being rebuild, display used available_build.
			if [[ ${stages[${child},rebuild]} == false ]] && [[ -n ${stages[${child},timestamp_available]} ]]; then
				for c in ${stages[${child},children]}; do
					if [[ ${stages[${c},rebuild]} = true ]]; then
						display_name="${display_name} (${stages[${child},timestamp_available]})"
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

# Converts remote repositories into local path to download them to.
# For local repositories it's just returning the same path.
repo_local_path() {
	local repository=${1}
	if [[ ${repository} == http://* || ${repository} == https://* ]]; then
		local local_name=$(echo ${repository} | sed 's|http[s]*://||' | sed -e 's/[^A-Za-z0-9._-]/_/g')
		echo ${overlays_cache_path}/${local_name}
	else
		echo ${repository}
	fi
}

binrepo_local_path() {
	local repository=${1}
	if [[ ${repository} == http://* || ${repository} == https://* || ${repository} == git@* ]]; then
		local local_name=$(echo ${repository} | sed 's|http[s]*://||' | sed 's|git@||' | sed -e 's/[^A-Za-z0-9._-]/_/g')
		echo ${binpkgs_cache_path}/remote/${local_name}
	else
		echo ${repository}
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
	arch      repos            common_flags chost
	cpu_flags compression_mode binrepo      binrepo_path
)
declare RELEASE_KEYS=( # Variables allowed in release.conf
	repos            common_flags chost        cpu_flags
	compression_mode binrepo      binrepo_path
)
declare STAGE_KEYS=( # Variables stored in stages[]
	kind

	arch_basearch arch_baseraw     arch_subarch
	arch_family   arch_interpreter arch_emulation

	platform release stage

	rel_type        target              source_subpath      product
	available_build timestamp_available timestamp_generated parent
	children

	chost common_flags cpu_flags

	treeish          overlays     overlays_local_paths binrepo     binrepo_local_path
	binrepo_path     binrepo_kind catalyst_conf        releng_base version_stamp
	compression_mode

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
binpkgs_cache_path=/var/cache/catalyst-lab/binpkgs
overlays_cache_path=/var/cache/catalyst-lab/overlays
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
	--upload-binrepos) UPLOAD_BINREPOS=true;; # Try to upload changes in binrepo after build finishes.
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
if [[ ${PREPARE} = true ]] || [[ ${--update-snapshot} = true ]]; then
	prepare_portage_snapshot
fi
if [[ ${PREPARE} = true ]] || [[ ${--update-releng} = true ]]; then
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

# TODO: Add lock file preventing multiple runs at once, but only if the same builds are involved (maybe).
# TODO: Add functions to manage platforms, releases and stages - add new, edit config, print config, etc.
# TODO: Add possibility to include shared files anywhere into spec files. So for example keep single list of basic installCD tools, and use them across all livecd specs.
# TODO: Make it possible to work with hubs (git based) - adding hub from github link, pulling automatically changes, registering in shared hub list, detecting name collisions.
# TODO: Check if settings common_flags is also only allowed in stage1
# TODO: Working with distcc (including local)
# TODO: Using remote binhosts
# TODO: Make possible setting different build sublocation (for building modified seeds) ?
# TODO: Add checking for valid config entries in config files
# TODO: Detect when profile changes in stage4 and if it does, automtically add rebuilds to fsscript file
# TODO: Define parent property for setting source_subpath. Parent can be name of stage, full name of stage (including platform and release) or remote. With remote if can just specify word remote and automatically find like, it it can specify tarball name or even full URL.
# TODO: Add support for binhost type jobs
# TODO: Add possibility to define remote jobs in templates. Automatically added remote jobs are considered "virtual"
