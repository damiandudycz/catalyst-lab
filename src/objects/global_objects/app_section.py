import inspect
from typing import ClassVar, Type
from dataclasses import dataclass
from gi.repository import Adw

@dataclass
class AppSection:
    cls: Type
    order: int
    label: str
    title: str
    icon: str
    show_in_side_bar: bool
    show_side_bar: bool

    # Registry
    all_sections: ClassVar[list[Type]] = []

def app_section(title: str, label: str | None = None, icon: str = "default-icon", show_in_side_bar: bool = True, show_side_bar: bool = True, order: int = 999_999_999):
    """app_section decorator."""
    def decorator(cls: Type):
        # Validate __init__ signature:
        init = cls.__init__
        sig = inspect.signature(init)
        params = list(sig.parameters.values())
        if (
            len(params) < 2
            or params[0].name != "self"
            or params[1].name != "content_navigation_view"
            or params[1].annotation is not Adw.NavigationView
            or not any(p.kind == p.VAR_KEYWORD for p in params)
        ):
            raise TypeError(f"{cls.__name__} must implement __init__(self, content_navigation_view: Adw.NavigationView, **kwargs)")

        # Store metadata:
        section = AppSection(
            cls=cls,
            order=order,
            label=label or title,
            title=title,
            icon=icon,
            show_in_side_bar=show_in_side_bar,
            show_side_bar=show_side_bar,
        )
        cls.section_details = section

        setattr(AppSection, cls.__name__, cls)
        AppSection.all_sections.append(cls)
        AppSection.all_sections.sort(key=lambda item: item.section_details.order)

        return cls
    return decorator

