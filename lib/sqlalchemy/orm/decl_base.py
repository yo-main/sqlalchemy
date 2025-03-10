# ext/declarative/base.py
# Copyright (C) 2005-2022 the SQLAlchemy authors and contributors
# <see AUTHORS file>
#
# This module is part of SQLAlchemy and is released under
# the MIT License: https://www.opensource.org/licenses/mit-license.php

"""Internal implementation for declarative."""

from __future__ import annotations

import collections
import dataclasses
import re
from typing import Any
from typing import Callable
from typing import cast
from typing import Dict
from typing import Iterable
from typing import List
from typing import Mapping
from typing import NoReturn
from typing import Optional
from typing import Sequence
from typing import Tuple
from typing import Type
from typing import TYPE_CHECKING
from typing import TypeVar
from typing import Union
import weakref

from . import attributes
from . import clsregistry
from . import exc as orm_exc
from . import instrumentation
from . import mapperlib
from ._typing import _O
from ._typing import attr_is_internal_proxy
from .attributes import InstrumentedAttribute
from .attributes import QueryableAttribute
from .base import _is_mapped_class
from .base import InspectionAttr
from .descriptor_props import CompositeProperty
from .descriptor_props import SynonymProperty
from .interfaces import _AttributeOptions
from .interfaces import _IntrospectsAnnotations
from .interfaces import _MappedAttribute
from .interfaces import _MapsColumns
from .interfaces import MapperProperty
from .mapper import Mapper as mapper
from .mapper import Mapper
from .properties import ColumnProperty
from .properties import MappedColumn
from .util import _extract_mapped_subtype
from .util import _is_mapped_annotation
from .util import class_mapper
from .. import event
from .. import exc
from .. import util
from ..sql import expression
from ..sql.base import _NoArg
from ..sql.schema import Column
from ..sql.schema import Table
from ..util import topological
from ..util.typing import _AnnotationScanType
from ..util.typing import Protocol
from ..util.typing import TypedDict
from ..util.typing import typing_get_args

if TYPE_CHECKING:
    from ._typing import _ClassDict
    from ._typing import _RegistryType
    from .decl_api import declared_attr
    from .instrumentation import ClassManager
    from ..sql.elements import NamedColumn
    from ..sql.schema import MetaData
    from ..sql.selectable import FromClause

_T = TypeVar("_T", bound=Any)

_MapperKwArgs = Mapping[str, Any]

_TableArgsType = Union[Tuple[Any, ...], Dict[str, Any]]


class _DeclMappedClassProtocol(Protocol[_O]):
    metadata: MetaData
    __mapper__: Mapper[_O]
    __table__: Table
    __tablename__: str
    __mapper_args__: Mapping[str, Any]
    __table_args__: Optional[_TableArgsType]

    _sa_apply_dc_transforms: Optional[_DataclassArguments]

    def __declare_first__(self) -> None:
        pass

    def __declare_last__(self) -> None:
        pass


class _DataclassArguments(TypedDict):
    init: Union[_NoArg, bool]
    repr: Union[_NoArg, bool]
    eq: Union[_NoArg, bool]
    order: Union[_NoArg, bool]
    unsafe_hash: Union[_NoArg, bool]
    match_args: Union[_NoArg, bool]
    kw_only: Union[_NoArg, bool]


def _declared_mapping_info(
    cls: Type[Any],
) -> Optional[Union[_DeferredMapperConfig, Mapper[Any]]]:
    # deferred mapping
    if _DeferredMapperConfig.has_cls(cls):
        return _DeferredMapperConfig.config_for_cls(cls)
    # regular mapping
    elif _is_mapped_class(cls):
        return class_mapper(cls, configure=False)
    else:
        return None


def _resolve_for_abstract_or_classical(cls: Type[Any]) -> Optional[Type[Any]]:
    if cls is object:
        return None

    sup: Optional[Type[Any]]

    if cls.__dict__.get("__abstract__", False):
        for base_ in cls.__bases__:
            sup = _resolve_for_abstract_or_classical(base_)
            if sup is not None:
                return sup
        else:
            return None
    else:
        clsmanager = _dive_for_cls_manager(cls)

        if clsmanager:
            return clsmanager.class_
        else:
            return cls


def _get_immediate_cls_attr(
    cls: Type[Any], attrname: str, strict: bool = False
) -> Optional[Any]:
    """return an attribute of the class that is either present directly
    on the class, e.g. not on a superclass, or is from a superclass but
    this superclass is a non-mapped mixin, that is, not a descendant of
    the declarative base and is also not classically mapped.

    This is used to detect attributes that indicate something about
    a mapped class independently from any mapped classes that it may
    inherit from.

    """

    # the rules are different for this name than others,
    # make sure we've moved it out.  transitional
    assert attrname != "__abstract__"

    if not issubclass(cls, object):
        return None

    if attrname in cls.__dict__:
        return getattr(cls, attrname)

    for base in cls.__mro__[1:]:
        _is_classicial_inherits = _dive_for_cls_manager(base) is not None

        if attrname in base.__dict__ and (
            base is cls
            or (
                (base in cls.__bases__ if strict else True)
                and not _is_classicial_inherits
            )
        ):
            return getattr(base, attrname)
    else:
        return None


def _dive_for_cls_manager(cls: Type[_O]) -> Optional[ClassManager[_O]]:
    # because the class manager registration is pluggable,
    # we need to do the search for every class in the hierarchy,
    # rather than just a simple "cls._sa_class_manager"

    for base in cls.__mro__:
        manager: Optional[ClassManager[_O]] = attributes.opt_manager_of_class(
            base
        )
        if manager:
            return manager
    return None


def _as_declarative(
    registry: _RegistryType, cls: Type[Any], dict_: _ClassDict
) -> Optional[_MapperConfig]:

    # declarative scans the class for attributes.  no table or mapper
    # args passed separately.
    return _MapperConfig.setup_mapping(registry, cls, dict_, None, {})


def _mapper(
    registry: _RegistryType,
    cls: Type[_O],
    table: Optional[FromClause],
    mapper_kw: _MapperKwArgs,
) -> Mapper[_O]:
    _ImperativeMapperConfig(registry, cls, table, mapper_kw)
    return cast("_DeclMappedClassProtocol[_O]", cls).__mapper__


@util.preload_module("sqlalchemy.orm.decl_api")
def _is_declarative_props(obj: Any) -> bool:
    declared_attr = util.preloaded.orm_decl_api.declared_attr

    return isinstance(obj, (declared_attr, util.classproperty))


def _check_declared_props_nocascade(
    obj: Any, name: str, cls: Type[_O]
) -> bool:
    if _is_declarative_props(obj):
        if getattr(obj, "_cascading", False):
            util.warn(
                "@declared_attr.cascading is not supported on the %s "
                "attribute on class %s.  This attribute invokes for "
                "subclasses in any case." % (name, cls)
            )
        return True
    else:
        return False


class _MapperConfig:
    __slots__ = (
        "cls",
        "classname",
        "properties",
        "declared_attr_reg",
        "__weakref__",
    )

    cls: Type[Any]
    classname: str
    properties: util.OrderedDict[
        str,
        Union[
            Sequence[NamedColumn[Any]], NamedColumn[Any], MapperProperty[Any]
        ],
    ]
    declared_attr_reg: Dict[declared_attr[Any], Any]

    @classmethod
    def setup_mapping(
        cls,
        registry: _RegistryType,
        cls_: Type[_O],
        dict_: _ClassDict,
        table: Optional[FromClause],
        mapper_kw: _MapperKwArgs,
    ) -> Optional[_MapperConfig]:
        manager = attributes.opt_manager_of_class(cls)
        if manager and manager.class_ is cls_:
            raise exc.InvalidRequestError(
                "Class %r already has been " "instrumented declaratively" % cls
            )

        if cls_.__dict__.get("__abstract__", False):
            return None

        defer_map = _get_immediate_cls_attr(
            cls_, "_sa_decl_prepare_nocascade", strict=True
        ) or hasattr(cls_, "_sa_decl_prepare")

        if defer_map:
            return _DeferredMapperConfig(
                registry, cls_, dict_, table, mapper_kw
            )
        else:
            return _ClassScanMapperConfig(
                registry, cls_, dict_, table, mapper_kw
            )

    def __init__(
        self,
        registry: _RegistryType,
        cls_: Type[Any],
        mapper_kw: _MapperKwArgs,
    ):
        self.cls = util.assert_arg_type(cls_, type, "cls_")
        self.classname = cls_.__name__
        self.properties = util.OrderedDict()
        self.declared_attr_reg = {}

        if not mapper_kw.get("non_primary", False):
            instrumentation.register_class(
                self.cls,
                finalize=False,
                registry=registry,
                declarative_scan=self,
                init_method=registry.constructor,
            )
        else:
            manager = attributes.opt_manager_of_class(self.cls)
            if not manager or not manager.is_mapped:
                raise exc.InvalidRequestError(
                    "Class %s has no primary mapper configured.  Configure "
                    "a primary mapper first before setting up a non primary "
                    "Mapper." % self.cls
                )

    def set_cls_attribute(self, attrname: str, value: _T) -> _T:

        manager = instrumentation.manager_of_class(self.cls)
        manager.install_member(attrname, value)
        return value

    def map(self, mapper_kw: _MapperKwArgs = ...) -> Mapper[Any]:
        raise NotImplementedError()

    def _early_mapping(self, mapper_kw: _MapperKwArgs) -> None:
        self.map(mapper_kw)


class _ImperativeMapperConfig(_MapperConfig):
    __slots__ = ("local_table", "inherits")

    def __init__(
        self,
        registry: _RegistryType,
        cls_: Type[_O],
        table: Optional[FromClause],
        mapper_kw: _MapperKwArgs,
    ):
        super(_ImperativeMapperConfig, self).__init__(
            registry, cls_, mapper_kw
        )

        self.local_table = self.set_cls_attribute("__table__", table)

        with mapperlib._CONFIGURE_MUTEX:
            if not mapper_kw.get("non_primary", False):
                clsregistry.add_class(
                    self.classname, self.cls, registry._class_registry
                )

            self._setup_inheritance(mapper_kw)

            self._early_mapping(mapper_kw)

    def map(self, mapper_kw: _MapperKwArgs = util.EMPTY_DICT) -> Mapper[Any]:
        mapper_cls = mapper

        return self.set_cls_attribute(
            "__mapper__",
            mapper_cls(self.cls, self.local_table, **mapper_kw),
        )

    def _setup_inheritance(self, mapper_kw: _MapperKwArgs) -> None:
        cls = self.cls

        inherits = mapper_kw.get("inherits", None)

        if inherits is None:
            # since we search for classical mappings now, search for
            # multiple mapped bases as well and raise an error.
            inherits_search = []
            for base_ in cls.__bases__:
                c = _resolve_for_abstract_or_classical(base_)
                if c is None:
                    continue
                if _declared_mapping_info(
                    c
                ) is not None and not _get_immediate_cls_attr(
                    c, "_sa_decl_prepare_nocascade", strict=True
                ):
                    inherits_search.append(c)

            if inherits_search:
                if len(inherits_search) > 1:
                    raise exc.InvalidRequestError(
                        "Class %s has multiple mapped bases: %r"
                        % (cls, inherits_search)
                    )
                inherits = inherits_search[0]
        elif isinstance(inherits, mapper):
            inherits = inherits.class_

        self.inherits = inherits


class _ClassScanMapperConfig(_MapperConfig):
    __slots__ = (
        "registry",
        "clsdict_view",
        "collected_attributes",
        "collected_annotations",
        "local_table",
        "persist_selectable",
        "declared_columns",
        "column_copies",
        "table_args",
        "tablename",
        "mapper_args",
        "mapper_args_fn",
        "inherits",
        "allow_dataclass_fields",
        "dataclass_setup_arguments",
        "is_dataclass_prior_to_mapping",
        "allow_unmapped_annotations",
    )

    registry: _RegistryType
    clsdict_view: _ClassDict
    collected_annotations: Dict[str, Tuple[Any, Any, Any, bool, Any]]
    collected_attributes: Dict[str, Any]
    local_table: Optional[FromClause]
    persist_selectable: Optional[FromClause]
    declared_columns: util.OrderedSet[Column[Any]]
    column_copies: Dict[
        Union[MappedColumn[Any], Column[Any]],
        Union[MappedColumn[Any], Column[Any]],
    ]
    tablename: Optional[str]
    mapper_args: Mapping[str, Any]
    table_args: Optional[_TableArgsType]
    mapper_args_fn: Optional[Callable[[], Dict[str, Any]]]
    inherits: Optional[Type[Any]]

    is_dataclass_prior_to_mapping: bool
    allow_unmapped_annotations: bool

    dataclass_setup_arguments: Optional[_DataclassArguments]
    """if the class has SQLAlchemy native dataclass parameters, where
    we will turn the class into a dataclass within the declarative mapping
    process.

    """

    allow_dataclass_fields: bool
    """if true, look for dataclass-processed Field objects on the target
    class as well as superclasses and extract ORM mapping directives from
    the "metadata" attribute of each Field.

    if False, dataclass fields can still be used, however they won't be
    mapped.

    """

    def __init__(
        self,
        registry: _RegistryType,
        cls_: Type[_O],
        dict_: _ClassDict,
        table: Optional[FromClause],
        mapper_kw: _MapperKwArgs,
    ):

        # grab class dict before the instrumentation manager has been added.
        # reduces cycles
        self.clsdict_view = (
            util.immutabledict(dict_) if dict_ else util.EMPTY_DICT
        )
        super(_ClassScanMapperConfig, self).__init__(registry, cls_, mapper_kw)
        self.registry = registry
        self.persist_selectable = None

        self.collected_attributes = {}
        self.collected_annotations = {}
        self.declared_columns = util.OrderedSet()
        self.column_copies = {}

        self.dataclass_setup_arguments = dca = getattr(
            self.cls, "_sa_apply_dc_transforms", None
        )

        self.allow_unmapped_annotations = getattr(
            self.cls, "__allow_unmapped__", False
        )

        self.is_dataclass_prior_to_mapping = cld = dataclasses.is_dataclass(
            cls_
        )

        sdk = _get_immediate_cls_attr(cls_, "__sa_dataclass_metadata_key__")

        # we don't want to consume Field objects from a not-already-dataclass.
        # the Field objects won't have their "name" or "type" populated,
        # and while it seems like we could just set these on Field as we
        # read them, Field is documented as "user read only" and we need to
        # stay far away from any off-label use of dataclasses APIs.
        if (not cld or dca) and sdk:
            raise exc.InvalidRequestError(
                "SQLAlchemy mapped dataclasses can't consume mapping "
                "information from dataclass.Field() objects if the immediate "
                "class is not already a dataclass."
            )

        # if already a dataclass, and __sa_dataclass_metadata_key__ present,
        # then also look inside of dataclass.Field() objects yielded by
        # dataclasses.get_fields(cls) when scanning for attributes
        self.allow_dataclass_fields = bool(sdk and cld)

        self._setup_declared_events()

        self._scan_attributes()

        self._setup_dataclasses_transforms()

        with mapperlib._CONFIGURE_MUTEX:
            clsregistry.add_class(
                self.classname, self.cls, registry._class_registry
            )

            self._extract_mappable_attributes()

            self._extract_declared_columns()

            self._setup_table(table)

            self._setup_inheritance(mapper_kw)

            self._early_mapping(mapper_kw)

    def _setup_declared_events(self) -> None:
        if _get_immediate_cls_attr(self.cls, "__declare_last__"):

            @event.listens_for(mapper, "after_configured")
            def after_configured() -> None:
                cast(
                    "_DeclMappedClassProtocol[Any]", self.cls
                ).__declare_last__()

        if _get_immediate_cls_attr(self.cls, "__declare_first__"):

            @event.listens_for(mapper, "before_configured")
            def before_configured() -> None:
                cast(
                    "_DeclMappedClassProtocol[Any]", self.cls
                ).__declare_first__()

    def _cls_attr_override_checker(
        self, cls: Type[_O]
    ) -> Callable[[str, Any], bool]:
        """Produce a function that checks if a class has overridden an
        attribute, taking SQLAlchemy-enabled dataclass fields into account.

        """

        if self.allow_dataclass_fields:
            sa_dataclass_metadata_key = _get_immediate_cls_attr(
                cls, "__sa_dataclass_metadata_key__"
            )
        else:
            sa_dataclass_metadata_key = None

        if not sa_dataclass_metadata_key:

            def attribute_is_overridden(key: str, obj: Any) -> bool:
                return getattr(cls, key) is not obj

        else:

            all_datacls_fields = {
                f.name: f.metadata[sa_dataclass_metadata_key]
                for f in util.dataclass_fields(cls)
                if sa_dataclass_metadata_key in f.metadata
            }
            local_datacls_fields = {
                f.name: f.metadata[sa_dataclass_metadata_key]
                for f in util.local_dataclass_fields(cls)
                if sa_dataclass_metadata_key in f.metadata
            }

            absent = object()

            def attribute_is_overridden(key: str, obj: Any) -> bool:
                if _is_declarative_props(obj):
                    obj = obj.fget

                # this function likely has some failure modes still if
                # someone is doing a deep mixing of the same attribute
                # name as plain Python attribute vs. dataclass field.

                ret = local_datacls_fields.get(key, absent)
                if _is_declarative_props(ret):
                    ret = ret.fget

                if ret is obj:
                    return False
                elif ret is not absent:
                    return True

                all_field = all_datacls_fields.get(key, absent)

                ret = getattr(cls, key, obj)

                if ret is obj:
                    return False

                # for dataclasses, this could be the
                # 'default' of the field.  so filter more specifically
                # for an already-mapped InstrumentedAttribute
                if ret is not absent and isinstance(
                    ret, InstrumentedAttribute
                ):
                    return True

                if all_field is obj:
                    return False
                elif all_field is not absent:
                    return True

                # can't find another attribute
                return False

        return attribute_is_overridden

    _skip_attrs = frozenset(
        [
            "__module__",
            "__annotations__",
            "__doc__",
            "__dict__",
            "__weakref__",
            "_sa_class_manager",
            "_sa_apply_dc_transforms",
            "__dict__",
            "__weakref__",
        ]
    )

    def _cls_attr_resolver(
        self, cls: Type[Any]
    ) -> Callable[[], Iterable[Tuple[str, Any, Any, bool]]]:
        """produce a function to iterate the "attributes" of a class,
        adjusting for SQLAlchemy fields embedded in dataclass fields.

        """
        cls_annotations = util.get_annotations(cls)

        cls_vars = vars(cls)

        skip = self._skip_attrs

        names = util.merge_lists_w_ordering(
            [n for n in cls_vars if n not in skip], list(cls_annotations)
        )

        if self.allow_dataclass_fields:
            sa_dataclass_metadata_key: Optional[str] = _get_immediate_cls_attr(
                cls, "__sa_dataclass_metadata_key__"
            )
        else:
            sa_dataclass_metadata_key = None

        if not sa_dataclass_metadata_key:

            def local_attributes_for_class() -> Iterable[
                Tuple[str, Any, Any, bool]
            ]:
                return (
                    (
                        name,
                        cls_vars.get(name),
                        cls_annotations.get(name),
                        False,
                    )
                    for name in names
                )

        else:
            dataclass_fields = {
                field.name: field for field in util.local_dataclass_fields(cls)
            }

            fixed_sa_dataclass_metadata_key = sa_dataclass_metadata_key

            def local_attributes_for_class() -> Iterable[
                Tuple[str, Any, Any, bool]
            ]:
                for name in names:
                    field = dataclass_fields.get(name, None)
                    if field and sa_dataclass_metadata_key in field.metadata:
                        yield field.name, _as_dc_declaredattr(
                            field.metadata, fixed_sa_dataclass_metadata_key
                        ), cls_annotations.get(field.name), True
                    else:
                        yield name, cls_vars.get(name), cls_annotations.get(
                            name
                        ), False

        return local_attributes_for_class

    def _scan_attributes(self) -> None:
        cls = self.cls

        cls_as_Decl = cast("_DeclMappedClassProtocol[Any]", cls)

        clsdict_view = self.clsdict_view
        collected_attributes = self.collected_attributes
        column_copies = self.column_copies
        mapper_args_fn = None
        table_args = inherited_table_args = None

        tablename = None
        fixed_table = "__table__" in clsdict_view

        attribute_is_overridden = self._cls_attr_override_checker(self.cls)

        bases = []

        for base in cls.__mro__:
            # collect bases and make sure standalone columns are copied
            # to be the column they will ultimately be on the class,
            # so that declared_attr functions use the right columns.
            # need to do this all the way up the hierarchy first
            # (see #8190)

            class_mapped = (
                base is not cls
                and _declared_mapping_info(base) is not None
                and not _get_immediate_cls_attr(
                    base, "_sa_decl_prepare_nocascade", strict=True
                )
            )

            local_attributes_for_class = self._cls_attr_resolver(base)

            if not class_mapped and base is not cls:
                locally_collected_columns = self._produce_column_copies(
                    local_attributes_for_class,
                    attribute_is_overridden,
                    fixed_table,
                )
            else:
                locally_collected_columns = {}

            bases.append(
                (
                    base,
                    class_mapped,
                    local_attributes_for_class,
                    locally_collected_columns,
                )
            )

        for (
            base,
            class_mapped,
            local_attributes_for_class,
            locally_collected_columns,
        ) in bases:

            # this transfer can also take place as we scan each name
            # for finer-grained control of how collected_attributes is
            # populated, as this is what impacts column ordering.
            # however it's simpler to get it out of the way here.
            collected_attributes.update(locally_collected_columns)

            for (
                name,
                obj,
                annotation,
                is_dataclass_field,
            ) in local_attributes_for_class():
                if re.match(r"^__.+__$", name):
                    if name == "__mapper_args__":
                        check_decl = _check_declared_props_nocascade(
                            obj, name, cls
                        )
                        if not mapper_args_fn and (
                            not class_mapped or check_decl
                        ):
                            # don't even invoke __mapper_args__ until
                            # after we've determined everything about the
                            # mapped table.
                            # make a copy of it so a class-level dictionary
                            # is not overwritten when we update column-based
                            # arguments.
                            def _mapper_args_fn() -> Dict[str, Any]:
                                return dict(cls_as_Decl.__mapper_args__)

                            mapper_args_fn = _mapper_args_fn

                    elif name == "__tablename__":
                        check_decl = _check_declared_props_nocascade(
                            obj, name, cls
                        )
                        if not tablename and (not class_mapped or check_decl):
                            tablename = cls_as_Decl.__tablename__
                    elif name == "__table_args__":
                        check_decl = _check_declared_props_nocascade(
                            obj, name, cls
                        )
                        if not table_args and (not class_mapped or check_decl):
                            table_args = cls_as_Decl.__table_args__
                            if not isinstance(
                                table_args, (tuple, dict, type(None))
                            ):
                                raise exc.ArgumentError(
                                    "__table_args__ value must be a tuple, "
                                    "dict, or None"
                                )
                            if base is not cls:
                                inherited_table_args = True
                    else:
                        # skip all other dunder names
                        continue
                elif class_mapped:
                    if _is_declarative_props(obj):
                        util.warn(
                            "Regular (i.e. not __special__) "
                            "attribute '%s.%s' uses @declared_attr, "
                            "but owning class %s is mapped - "
                            "not applying to subclass %s."
                            % (base.__name__, name, base, cls)
                        )

                    continue
                elif base is not cls:
                    # we're a mixin, abstract base, or something that is
                    # acting like that for now.

                    if isinstance(obj, (Column, MappedColumn)):
                        # already copied columns to the mapped class.
                        continue
                    elif isinstance(obj, MapperProperty):
                        raise exc.InvalidRequestError(
                            "Mapper properties (i.e. deferred,"
                            "column_property(), relationship(), etc.) must "
                            "be declared as @declared_attr callables "
                            "on declarative mixin classes.  For dataclass "
                            "field() objects, use a lambda:"
                        )
                    elif _is_declarative_props(obj):
                        # tried to get overloads to tell this to
                        # pylance, no luck
                        assert obj is not None

                        if obj._cascading:
                            if name in clsdict_view:
                                # unfortunately, while we can use the user-
                                # defined attribute here to allow a clean
                                # override, if there's another
                                # subclass below then it still tries to use
                                # this.  not sure if there is enough
                                # information here to add this as a feature
                                # later on.
                                util.warn(
                                    "Attribute '%s' on class %s cannot be "
                                    "processed due to "
                                    "@declared_attr.cascading; "
                                    "skipping" % (name, cls)
                                )
                            collected_attributes[name] = column_copies[
                                obj
                            ] = ret = obj.__get__(obj, cls)
                            setattr(cls, name, ret)
                        else:
                            if is_dataclass_field:
                                # access attribute using normal class access
                                # first, to see if it's been mapped on a
                                # superclass.   note if the dataclasses.field()
                                # has "default", this value can be anything.
                                ret = getattr(cls, name, None)

                                # so, if it's anything that's not ORM
                                # mapped, assume we should invoke the
                                # declared_attr
                                if not isinstance(ret, InspectionAttr):
                                    ret = obj.fget()
                            else:
                                # access attribute using normal class access.
                                # if the declared attr already took place
                                # on a superclass that is mapped, then
                                # this is no longer a declared_attr, it will
                                # be the InstrumentedAttribute
                                ret = getattr(cls, name)

                            # correct for proxies created from hybrid_property
                            # or similar.  note there is no known case that
                            # produces nested proxies, so we are only
                            # looking one level deep right now.

                            if (
                                isinstance(ret, InspectionAttr)
                                and attr_is_internal_proxy(ret)
                                and not isinstance(
                                    ret.original_property, MapperProperty
                                )
                            ):
                                ret = ret.descriptor

                            collected_attributes[name] = column_copies[
                                obj
                            ] = ret

                        if (
                            isinstance(ret, (Column, MapperProperty))
                            and ret.doc is None
                        ):
                            ret.doc = obj.__doc__

                        self._collect_annotation(
                            name,
                            obj._collect_return_annotation(),
                            True,
                            obj,
                        )
                    elif _is_mapped_annotation(annotation, cls):
                        # Mapped annotation without any object.
                        # product_column_copies should have handled this.
                        # if future support for other MapperProperty,
                        # then test if this name is already handled and
                        # otherwise proceed to generate.
                        if not fixed_table:
                            assert name in collected_attributes
                        continue
                    else:
                        # here, the attribute is some other kind of
                        # property that we assume is not part of the
                        # declarative mapping.  however, check for some
                        # more common mistakes
                        self._warn_for_decl_attributes(base, name, obj)
                elif is_dataclass_field and (
                    name not in clsdict_view or clsdict_view[name] is not obj
                ):
                    # here, we are definitely looking at the target class
                    # and not a superclass.   this is currently a
                    # dataclass-only path.  if the name is only
                    # a dataclass field and isn't in local cls.__dict__,
                    # put the object there.
                    # assert that the dataclass-enabled resolver agrees
                    # with what we are seeing

                    assert not attribute_is_overridden(name, obj)

                    if _is_declarative_props(obj):
                        obj = obj.fget()

                    collected_attributes[name] = obj
                    self._collect_annotation(name, annotation, False, obj)
                else:
                    generated_obj = self._collect_annotation(
                        name, annotation, None, obj
                    )
                    if (
                        obj is None
                        and not fixed_table
                        and _is_mapped_annotation(annotation, cls)
                    ):
                        collected_attributes[name] = (
                            generated_obj
                            if generated_obj is not None
                            else MappedColumn()
                        )
                    elif name in clsdict_view:
                        collected_attributes[name] = obj
                    # else if the name is not in the cls.__dict__,
                    # don't collect it as an attribute.
                    # we will see the annotation only, which is meaningful
                    # both for mapping and dataclasses setup

        if inherited_table_args and not tablename:
            table_args = None

        self.table_args = table_args
        self.tablename = tablename
        self.mapper_args_fn = mapper_args_fn

    def _setup_dataclasses_transforms(self) -> None:

        dataclass_setup_arguments = self.dataclass_setup_arguments
        if not dataclass_setup_arguments:
            return

        manager = instrumentation.manager_of_class(self.cls)
        assert manager is not None

        field_list = [
            _AttributeOptions._get_arguments_for_make_dataclass(
                key,
                anno,
                mapped_container,
                self.collected_attributes.get(key, _NoArg.NO_ARG),
            )
            for key, anno, mapped_container in (
                (
                    key,
                    mapped_anno if mapped_anno else raw_anno,
                    mapped_container,
                )
                for key, (
                    raw_anno,
                    mapped_container,
                    mapped_anno,
                    is_dc,
                    attr_value,
                ) in self.collected_annotations.items()
            )
        ]
        annotations = {}
        defaults = {}
        for item in field_list:
            if len(item) == 2:
                name, tp = item  # type: ignore
            elif len(item) == 3:
                name, tp, spec = item  # type: ignore
                defaults[name] = spec
            else:
                assert False
            annotations[name] = tp

        for k, v in defaults.items():
            setattr(self.cls, k, v)

        self.cls.__annotations__ = annotations

        self._assert_dc_arguments(dataclass_setup_arguments)

        dataclasses.dataclass(
            self.cls,
            **{
                k: v
                for k, v in dataclass_setup_arguments.items()
                if v is not _NoArg.NO_ARG
            },
        )

    @classmethod
    def _assert_dc_arguments(cls, arguments: _DataclassArguments) -> None:
        allowed = {
            "init",
            "repr",
            "order",
            "eq",
            "unsafe_hash",
            "kw_only",
            "match_args",
        }
        disallowed_args = set(arguments).difference(allowed)
        if disallowed_args:
            msg = ", ".join(f"{arg!r}" for arg in sorted(disallowed_args))
            raise exc.ArgumentError(
                f"Dataclass argument(s) {msg} are not accepted"
            )

    def _collect_annotation(
        self,
        name: str,
        raw_annotation: _AnnotationScanType,
        expect_mapped: Optional[bool],
        attr_value: Any,
    ) -> Any:

        if name in self.collected_annotations:
            return self.collected_annotations[name][4]

        if raw_annotation is None:
            return attr_value

        is_dataclass = self.is_dataclass_prior_to_mapping
        allow_unmapped = self.allow_unmapped_annotations

        if expect_mapped is None:
            is_dataclass_field = isinstance(attr_value, dataclasses.Field)
            expect_mapped = (
                not is_dataclass_field
                and not allow_unmapped
                and (
                    attr_value is None
                    or isinstance(attr_value, _MappedAttribute)
                )
            )
        else:
            is_dataclass_field = False

        is_dataclass_field = False
        extracted = _extract_mapped_subtype(
            raw_annotation,
            self.cls,
            name,
            type(attr_value),
            required=False,
            is_dataclass_field=is_dataclass_field,
            expect_mapped=expect_mapped
            and not is_dataclass,  # self.allow_dataclass_fields,
        )

        if extracted is None:
            # ClassVar can come out here
            return attr_value

        extracted_mapped_annotation, mapped_container = extracted

        if attr_value is None:
            for elem in typing_get_args(extracted_mapped_annotation):
                # look in Annotated[...] for an ORM construct,
                # such as Annotated[int, mapped_column(primary_key=True)]
                if isinstance(elem, _IntrospectsAnnotations):
                    attr_value = elem.found_in_pep593_annotated()

        self.collected_annotations[name] = (
            raw_annotation,
            mapped_container,
            extracted_mapped_annotation,
            is_dataclass,
            attr_value,
        )
        return attr_value

    def _warn_for_decl_attributes(
        self, cls: Type[Any], key: str, c: Any
    ) -> None:
        if isinstance(c, expression.ColumnClause):
            util.warn(
                f"Attribute '{key}' on class {cls} appears to "
                "be a non-schema 'sqlalchemy.sql.column()' "
                "object; this won't be part of the declarative mapping"
            )

    def _produce_column_copies(
        self,
        attributes_for_class: Callable[
            [], Iterable[Tuple[str, Any, Any, bool]]
        ],
        attribute_is_overridden: Callable[[str, Any], bool],
        fixed_table: bool,
    ) -> Dict[str, Union[Column[Any], MappedColumn[Any]]]:
        cls = self.cls
        dict_ = self.clsdict_view
        locally_collected_attributes = {}
        column_copies = self.column_copies
        # copy mixin columns to the mapped class

        for name, obj, annotation, is_dataclass in attributes_for_class():
            if (
                not fixed_table
                and obj is None
                and _is_mapped_annotation(annotation, cls)
            ):
                obj = self._collect_annotation(name, annotation, True, obj)
                if obj is None:
                    obj = MappedColumn()

                locally_collected_attributes[name] = obj
                setattr(cls, name, obj)

            elif isinstance(obj, (Column, MappedColumn)):

                if attribute_is_overridden(name, obj):
                    # if column has been overridden
                    # (like by the InstrumentedAttribute of the
                    # superclass), skip.  don't collect the annotation
                    # either (issue #8718)
                    continue

                obj = self._collect_annotation(name, annotation, True, obj)

                if name not in dict_ and not (
                    "__table__" in dict_
                    and (getattr(obj, "name", None) or name)
                    in dict_["__table__"].c
                ):
                    if obj.foreign_keys:
                        for fk in obj.foreign_keys:
                            if (
                                fk._table_column is not None
                                and fk._table_column.table is None
                            ):
                                raise exc.InvalidRequestError(
                                    "Columns with foreign keys to "
                                    "non-table-bound "
                                    "columns must be declared as "
                                    "@declared_attr callables "
                                    "on declarative mixin classes.  "
                                    "For dataclass "
                                    "field() objects, use a lambda:."
                                )

                    column_copies[obj] = copy_ = obj._copy()

                    locally_collected_attributes[name] = copy_
                    setattr(cls, name, copy_)

        return locally_collected_attributes

    def _extract_mappable_attributes(self) -> None:
        cls = self.cls
        collected_attributes = self.collected_attributes

        our_stuff = self.properties

        late_mapped = _get_immediate_cls_attr(
            cls, "_sa_decl_prepare_nocascade", strict=True
        )

        expect_annotations_wo_mapped = (
            self.allow_unmapped_annotations
            or self.is_dataclass_prior_to_mapping
        )

        for k in list(collected_attributes):

            if k in ("__table__", "__tablename__", "__mapper_args__"):
                continue

            value = collected_attributes[k]

            if _is_declarative_props(value):
                # @declared_attr in collected_attributes only occurs here for a
                # @declared_attr that's directly on the mapped class;
                # for a mixin, these have already been evaluated
                if value._cascading:
                    util.warn(
                        "Use of @declared_attr.cascading only applies to "
                        "Declarative 'mixin' and 'abstract' classes.  "
                        "Currently, this flag is ignored on mapped class "
                        "%s" % self.cls
                    )

                value = getattr(cls, k)

            elif (
                isinstance(value, QueryableAttribute)
                and value.class_ is not cls
                and value.key != k
            ):
                # detect a QueryableAttribute that's already mapped being
                # assigned elsewhere in userland, turn into a synonym()
                value = SynonymProperty(value.key)
                setattr(cls, k, value)

            if (
                isinstance(value, tuple)
                and len(value) == 1
                and isinstance(value[0], (Column, _MappedAttribute))
            ):
                util.warn(
                    "Ignoring declarative-like tuple value of attribute "
                    "'%s': possibly a copy-and-paste error with a comma "
                    "accidentally placed at the end of the line?" % k
                )
                continue
            elif not isinstance(value, (Column, MapperProperty, _MapsColumns)):
                # using @declared_attr for some object that
                # isn't Column/MapperProperty; remove from the clsdict_view
                # and place the evaluated value onto the class.
                if not k.startswith("__"):
                    collected_attributes.pop(k)
                    self._warn_for_decl_attributes(cls, k, value)
                    if not late_mapped:
                        setattr(cls, k, value)
                continue
            # we expect to see the name 'metadata' in some valid cases;
            # however at this point we see it's assigned to something trying
            # to be mapped, so raise for that.
            elif k == "metadata":
                raise exc.InvalidRequestError(
                    "Attribute name 'metadata' is reserved "
                    "for the MetaData instance when using a "
                    "declarative base class."
                )
            elif isinstance(value, Column):
                _undefer_column_name(
                    k, self.column_copies.get(value, value)  # type: ignore
                )
            else:
                if isinstance(value, _IntrospectsAnnotations):
                    (
                        annotation,
                        mapped_container,
                        extracted_mapped_annotation,
                        is_dataclass,
                        attr_value,
                    ) = self.collected_annotations.get(
                        k, (None, None, None, False, None)
                    )

                    # issue #8692 - don't do any annotation interpretation if
                    # an annotation were present and a container such as
                    # Mapped[] etc. were not used.  If annotation is None,
                    # do declarative_scan so that the property can raise
                    # for required
                    if mapped_container is not None or annotation is None:
                        value.declarative_scan(
                            self.registry,
                            cls,
                            k,
                            mapped_container,
                            annotation,
                            extracted_mapped_annotation,
                            is_dataclass,
                        )
                    else:
                        # assert that we were expecting annotations
                        # without Mapped[] were going to be passed.
                        # otherwise an error should have been raised
                        # by util._extract_mapped_subtype before we got here.
                        assert expect_annotations_wo_mapped

                if (
                    isinstance(value, (MapperProperty, _MapsColumns))
                    and value._has_dataclass_arguments
                    and not self.dataclass_setup_arguments
                ):
                    if isinstance(value, MapperProperty):
                        argnames = [
                            "init",
                            "default_factory",
                            "repr",
                            "default",
                        ]
                    else:
                        argnames = ["init", "default_factory", "repr"]

                    args = {
                        a
                        for a in argnames
                        if getattr(
                            value._attribute_options, f"dataclasses_{a}"
                        )
                        is not _NoArg.NO_ARG
                    }
                    raise exc.ArgumentError(
                        f"Attribute '{k}' on class {cls} includes dataclasses "
                        f"argument(s): "
                        f"{', '.join(sorted(repr(a) for a in args))} but "
                        f"class does not specify "
                        "SQLAlchemy native dataclass configuration."
                    )

            our_stuff[k] = value

    def _extract_declared_columns(self) -> None:
        our_stuff = self.properties

        # extract columns from the class dict
        declared_columns = self.declared_columns
        name_to_prop_key = collections.defaultdict(set)

        for key, c in list(our_stuff.items()):
            if isinstance(c, _MapsColumns):

                mp_to_assign = c.mapper_property_to_assign
                if mp_to_assign:
                    our_stuff[key] = mp_to_assign
                else:
                    # if no mapper property to assign, this currently means
                    # this is a MappedColumn that will produce a Column for us
                    del our_stuff[key]

                for col in c.columns_to_assign:
                    if not isinstance(c, CompositeProperty):
                        name_to_prop_key[col.name].add(key)
                    declared_columns.add(col)

                    # if this is a MappedColumn and the attribute key we
                    # have is not what the column has for its key, map the
                    # Column explicitly under the attribute key name.
                    # otherwise, Mapper will map it under the column key.
                    if mp_to_assign is None and key != col.key:
                        our_stuff[key] = col
            elif isinstance(c, Column):
                # undefer previously occurred here, and now occurs earlier.
                # ensure every column we get here has been named
                assert c.name is not None
                name_to_prop_key[c.name].add(key)
                declared_columns.add(c)
                # if the column is the same name as the key,
                # remove it from the explicit properties dict.
                # the normal rules for assigning column-based properties
                # will take over, including precedence of columns
                # in multi-column ColumnProperties.
                if key == c.key:
                    del our_stuff[key]

        for name, keys in name_to_prop_key.items():
            if len(keys) > 1:
                util.warn(
                    "On class %r, Column object %r named "
                    "directly multiple times, "
                    "only one will be used: %s. "
                    "Consider using orm.synonym instead"
                    % (self.classname, name, (", ".join(sorted(keys))))
                )

    def _setup_table(self, table: Optional[FromClause] = None) -> None:
        cls = self.cls
        cls_as_Decl = cast("_DeclMappedClassProtocol[Any]", cls)

        tablename = self.tablename
        table_args = self.table_args
        clsdict_view = self.clsdict_view
        declared_columns = self.declared_columns

        manager = attributes.manager_of_class(cls)

        if "__table__" not in clsdict_view and table is None:
            if hasattr(cls, "__table_cls__"):
                table_cls = cast(
                    Type[Table],
                    util.unbound_method_to_callable(cls.__table_cls__),  # type: ignore  # noqa: E501
                )
            else:
                table_cls = Table

            if tablename is not None:

                args: Tuple[Any, ...] = ()
                table_kw: Dict[str, Any] = {}

                if table_args:
                    if isinstance(table_args, dict):
                        table_kw = table_args
                    elif isinstance(table_args, tuple):
                        if isinstance(table_args[-1], dict):
                            args, table_kw = table_args[0:-1], table_args[-1]
                        else:
                            args = table_args

                autoload_with = clsdict_view.get("__autoload_with__")
                if autoload_with:
                    table_kw["autoload_with"] = autoload_with

                autoload = clsdict_view.get("__autoload__")
                if autoload:
                    table_kw["autoload"] = True

                table = self.set_cls_attribute(
                    "__table__",
                    table_cls(
                        tablename,
                        self._metadata_for_cls(manager),
                        *(tuple(declared_columns) + tuple(args)),
                        **table_kw,
                    ),
                )
        else:
            if table is None:
                table = cls_as_Decl.__table__
            if declared_columns:
                for c in declared_columns:
                    if not table.c.contains_column(c):
                        raise exc.ArgumentError(
                            "Can't add additional column %r when "
                            "specifying __table__" % c.key
                        )

        self.local_table = table

    def _metadata_for_cls(self, manager: ClassManager[Any]) -> MetaData:
        if hasattr(self.cls, "metadata"):
            return cast("_DeclMappedClassProtocol[Any]", self.cls).metadata
        else:
            return manager.registry.metadata

    def _setup_inheritance(self, mapper_kw: _MapperKwArgs) -> None:
        table = self.local_table
        cls = self.cls
        table_args = self.table_args
        declared_columns = self.declared_columns

        inherits = mapper_kw.get("inherits", None)

        if inherits is None:
            # since we search for classical mappings now, search for
            # multiple mapped bases as well and raise an error.
            inherits_search = []
            for base_ in cls.__bases__:
                c = _resolve_for_abstract_or_classical(base_)
                if c is None:
                    continue
                if _declared_mapping_info(
                    c
                ) is not None and not _get_immediate_cls_attr(
                    c, "_sa_decl_prepare_nocascade", strict=True
                ):
                    if c not in inherits_search:
                        inherits_search.append(c)

            if inherits_search:
                if len(inherits_search) > 1:
                    raise exc.InvalidRequestError(
                        "Class %s has multiple mapped bases: %r"
                        % (cls, inherits_search)
                    )
                inherits = inherits_search[0]
        elif isinstance(inherits, mapper):
            inherits = inherits.class_

        self.inherits = inherits

        if (
            table is None
            and self.inherits is None
            and not _get_immediate_cls_attr(cls, "__no_table__")
        ):

            raise exc.InvalidRequestError(
                "Class %r does not have a __table__ or __tablename__ "
                "specified and does not inherit from an existing "
                "table-mapped class." % cls
            )
        elif self.inherits:
            inherited_mapper_or_config = _declared_mapping_info(self.inherits)
            assert inherited_mapper_or_config is not None
            inherited_table = inherited_mapper_or_config.local_table
            inherited_persist_selectable = (
                inherited_mapper_or_config.persist_selectable
            )

            if table is None:
                # single table inheritance.
                # ensure no table args
                if table_args:
                    raise exc.ArgumentError(
                        "Can't place __table_args__ on an inherited class "
                        "with no table."
                    )

                # add any columns declared here to the inherited table.
                if declared_columns and not isinstance(inherited_table, Table):
                    raise exc.ArgumentError(
                        f"Can't declare columns on single-table-inherited "
                        f"subclass {self.cls}; superclass {self.inherits} "
                        "is not mapped to a Table"
                    )

                for col in declared_columns:
                    assert inherited_table is not None
                    if col.name in inherited_table.c:
                        if inherited_table.c[col.name] is col:
                            continue
                        raise exc.ArgumentError(
                            "Column '%s' on class %s conflicts with "
                            "existing column '%s'"
                            % (col, cls, inherited_table.c[col.name])
                        )
                    if col.primary_key:
                        raise exc.ArgumentError(
                            "Can't place primary key columns on an inherited "
                            "class with no table."
                        )

                    if TYPE_CHECKING:
                        assert isinstance(inherited_table, Table)

                    inherited_table.append_column(col)
                    if (
                        inherited_persist_selectable is not None
                        and inherited_persist_selectable is not inherited_table
                    ):
                        inherited_persist_selectable._refresh_for_new_column(
                            col
                        )

    def _prepare_mapper_arguments(self, mapper_kw: _MapperKwArgs) -> None:
        properties = self.properties

        if self.mapper_args_fn:
            mapper_args = self.mapper_args_fn()
        else:
            mapper_args = {}

        if mapper_kw:
            mapper_args.update(mapper_kw)

        if "properties" in mapper_args:
            properties = dict(properties)
            properties.update(mapper_args["properties"])

        # make sure that column copies are used rather
        # than the original columns from any mixins
        for k in ("version_id_col", "polymorphic_on"):
            if k in mapper_args:
                v = mapper_args[k]
                mapper_args[k] = self.column_copies.get(v, v)

        if "inherits" in mapper_args:
            inherits_arg = mapper_args["inherits"]
            if isinstance(inherits_arg, mapper):
                inherits_arg = inherits_arg.class_

            if inherits_arg is not self.inherits:
                raise exc.InvalidRequestError(
                    "mapper inherits argument given for non-inheriting "
                    "class %s" % (mapper_args["inherits"])
                )

        if self.inherits:
            mapper_args["inherits"] = self.inherits

        if self.inherits and not mapper_args.get("concrete", False):
            # single or joined inheritance
            # exclude any cols on the inherited table which are
            # not mapped on the parent class, to avoid
            # mapping columns specific to sibling/nephew classes
            inherited_mapper = _declared_mapping_info(self.inherits)
            assert isinstance(inherited_mapper, Mapper)
            inherited_table = inherited_mapper.local_table

            if "exclude_properties" not in mapper_args:
                mapper_args["exclude_properties"] = exclude_properties = set(
                    [
                        c.key
                        for c in inherited_table.c
                        if c not in inherited_mapper._columntoproperty
                    ]
                ).union(inherited_mapper.exclude_properties or ())
                exclude_properties.difference_update(
                    [c.key for c in self.declared_columns]
                )

            # look through columns in the current mapper that
            # are keyed to a propname different than the colname
            # (if names were the same, we'd have popped it out above,
            # in which case the mapper makes this combination).
            # See if the superclass has a similar column property.
            # If so, join them together.
            for k, col in list(properties.items()):
                if not isinstance(col, expression.ColumnElement):
                    continue
                if k in inherited_mapper._props:
                    p = inherited_mapper._props[k]
                    if isinstance(p, ColumnProperty):
                        # note here we place the subclass column
                        # first.  See [ticket:1892] for background.
                        properties[k] = [col] + p.columns
        result_mapper_args = mapper_args.copy()
        result_mapper_args["properties"] = properties
        self.mapper_args = result_mapper_args

    def map(self, mapper_kw: _MapperKwArgs = util.EMPTY_DICT) -> Mapper[Any]:
        self._prepare_mapper_arguments(mapper_kw)
        if hasattr(self.cls, "__mapper_cls__"):
            mapper_cls = cast(
                "Type[Mapper[Any]]",
                util.unbound_method_to_callable(
                    self.cls.__mapper_cls__  # type: ignore
                ),
            )
        else:
            mapper_cls = mapper

        return self.set_cls_attribute(
            "__mapper__",
            mapper_cls(self.cls, self.local_table, **self.mapper_args),
        )


@util.preload_module("sqlalchemy.orm.decl_api")
def _as_dc_declaredattr(
    field_metadata: Mapping[str, Any], sa_dataclass_metadata_key: str
) -> Any:
    # wrap lambdas inside dataclass fields inside an ad-hoc declared_attr.
    # we can't write it because field.metadata is immutable :( so we have
    # to go through extra trouble to compare these
    decl_api = util.preloaded.orm_decl_api
    obj = field_metadata[sa_dataclass_metadata_key]
    if callable(obj) and not isinstance(obj, decl_api.declared_attr):
        return decl_api.declared_attr(obj)
    else:
        return obj


class _DeferredMapperConfig(_ClassScanMapperConfig):
    _cls: weakref.ref[Type[Any]]

    _configs: util.OrderedDict[
        weakref.ref[Type[Any]], _DeferredMapperConfig
    ] = util.OrderedDict()

    def _early_mapping(self, mapper_kw: _MapperKwArgs) -> None:
        pass

    # mypy disallows plain property override of variable
    @property  # type: ignore
    def cls(self) -> Type[Any]:  # type: ignore
        return self._cls()  # type: ignore

    @cls.setter
    def cls(self, class_: Type[Any]) -> None:
        self._cls = weakref.ref(class_, self._remove_config_cls)
        self._configs[self._cls] = self

    @classmethod
    def _remove_config_cls(cls, ref: weakref.ref[Type[Any]]) -> None:
        cls._configs.pop(ref, None)

    @classmethod
    def has_cls(cls, class_: Type[Any]) -> bool:
        # 2.6 fails on weakref if class_ is an old style class
        return isinstance(class_, type) and weakref.ref(class_) in cls._configs

    @classmethod
    def raise_unmapped_for_cls(cls, class_: Type[Any]) -> NoReturn:
        if hasattr(class_, "_sa_raise_deferred_config"):
            class_._sa_raise_deferred_config()  # type: ignore

        raise orm_exc.UnmappedClassError(
            class_,
            msg=(
                f"Class {orm_exc._safe_cls_name(class_)} has a deferred "
                "mapping on it.  It is not yet usable as a mapped class."
            ),
        )

    @classmethod
    def config_for_cls(cls, class_: Type[Any]) -> _DeferredMapperConfig:
        return cls._configs[weakref.ref(class_)]

    @classmethod
    def classes_for_base(
        cls, base_cls: Type[Any], sort: bool = True
    ) -> List[_DeferredMapperConfig]:
        classes_for_base = [
            m
            for m, cls_ in [(m, m.cls) for m in cls._configs.values()]
            if cls_ is not None and issubclass(cls_, base_cls)
        ]

        if not sort:
            return classes_for_base

        all_m_by_cls = dict((m.cls, m) for m in classes_for_base)

        tuples: List[Tuple[_DeferredMapperConfig, _DeferredMapperConfig]] = []
        for m_cls in all_m_by_cls:
            tuples.extend(
                (all_m_by_cls[base_cls], all_m_by_cls[m_cls])
                for base_cls in m_cls.__bases__
                if base_cls in all_m_by_cls
            )
        return list(topological.sort(tuples, classes_for_base))

    def map(self, mapper_kw: _MapperKwArgs = util.EMPTY_DICT) -> Mapper[Any]:
        self._configs.pop(self._cls, None)
        return super(_DeferredMapperConfig, self).map(mapper_kw)


def _add_attribute(
    cls: Type[Any], key: str, value: MapperProperty[Any]
) -> None:
    """add an attribute to an existing declarative class.

    This runs through the logic to determine MapperProperty,
    adds it to the Mapper, adds a column to the mapped Table, etc.

    """

    if "__mapper__" in cls.__dict__:
        mapped_cls = cast("_DeclMappedClassProtocol[Any]", cls)
        if isinstance(value, Column):
            _undefer_column_name(key, value)
            # TODO: raise for this is not a Table
            mapped_cls.__table__.append_column(value, replace_existing=True)
            mapped_cls.__mapper__.add_property(key, value)
        elif isinstance(value, _MapsColumns):
            mp = value.mapper_property_to_assign
            for col in value.columns_to_assign:
                _undefer_column_name(key, col)
                # TODO: raise for this is not a Table
                mapped_cls.__table__.append_column(col, replace_existing=True)
                if not mp:
                    mapped_cls.__mapper__.add_property(key, col)
            if mp:
                mapped_cls.__mapper__.add_property(key, mp)
        elif isinstance(value, MapperProperty):
            mapped_cls.__mapper__.add_property(key, value)
        elif isinstance(value, QueryableAttribute) and value.key != key:
            # detect a QueryableAttribute that's already mapped being
            # assigned elsewhere in userland, turn into a synonym()
            value = SynonymProperty(value.key)
            mapped_cls.__mapper__.add_property(key, value)
        else:
            type.__setattr__(cls, key, value)
            mapped_cls.__mapper__._expire_memoizations()
    else:
        type.__setattr__(cls, key, value)


def _del_attribute(cls: Type[Any], key: str) -> None:

    if (
        "__mapper__" in cls.__dict__
        and key in cls.__dict__
        and not cast(
            "_DeclMappedClassProtocol[Any]", cls
        ).__mapper__._dispose_called
    ):
        value = cls.__dict__[key]
        if isinstance(
            value, (Column, _MapsColumns, MapperProperty, QueryableAttribute)
        ):
            raise NotImplementedError(
                "Can't un-map individual mapped attributes on a mapped class."
            )
        else:
            type.__delattr__(cls, key)
            cast(
                "_DeclMappedClassProtocol[Any]", cls
            ).__mapper__._expire_memoizations()
    else:
        type.__delattr__(cls, key)


def _declarative_constructor(self: Any, **kwargs: Any) -> None:
    """A simple constructor that allows initialization from kwargs.

    Sets attributes on the constructed instance using the names and
    values in ``kwargs``.

    Only keys that are present as
    attributes of the instance's class are allowed. These could be,
    for example, any mapped columns or relationships.
    """
    cls_ = type(self)
    for k in kwargs:
        if not hasattr(cls_, k):
            raise TypeError(
                "%r is an invalid keyword argument for %s" % (k, cls_.__name__)
            )
        setattr(self, k, kwargs[k])


_declarative_constructor.__name__ = "__init__"


def _undefer_column_name(key: str, column: Column[Any]) -> None:
    if column.key is None:
        column.key = key
    if column.name is None:
        column.name = key
