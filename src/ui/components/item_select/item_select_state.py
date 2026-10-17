class ItemSelectionData:
    """Shared item storage for repository-backed selectors."""

    def set_static_list(self, items: list):
        self.static_list = items
        self._load_items()

    def set_leading_items(self, items: list):
        self.leading_items = items
        if hasattr(self, "rows"):
            self._load_items()

    def items(self) -> list:
        items = self.static_list if hasattr(self, "static_list") else self.repository.value if hasattr(self, "repository") else []
        return getattr(self, "leading_items", []) + list(items)


def no_valid_items_visible(valid_items, display_none: bool, allow_multiselect: bool = False,
                            has_supported_selection: bool = False) -> bool:
    """Show the empty state when no choices are selectable or already selected as supported."""
    none_is_option = display_none and not allow_multiselect
    return not valid_items and not none_is_option and not has_supported_selection
