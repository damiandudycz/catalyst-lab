def no_valid_items_visible(valid_items, display_none: bool, allow_multiselect: bool = False,
                            has_supported_selection: bool = False) -> bool:
    """Show the empty state when no choices are selectable or already selected as supported."""
    none_is_option = display_none and not allow_multiselect
    return not valid_items and not none_is_option and not has_supported_selection
