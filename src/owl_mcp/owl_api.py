import hashlib
import logging
import os
import re
import shutil
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Optional

import pyhornedowl
from pyhornedowl.model import Import, OntologyAnnotation
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from owl_mcp.format_utils import guess_serialization
from owl_mcp.xml_catalog_utils import read_catalog

from .axiom_parser import parse_axiom_string, serialize_axioms

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("simple-owl-api")

MAKEFILE_NORMALIZE_RULE = "normalize_src"


class SimpleOwlAPI:
    """
    A simple Ontology object for working with OWL ontologies using functional syntax strings.

    This class provides a simplified interface to py-horned-owl, where axioms can be
    manipulated as strings in OWL functional syntax, avoiding the need to work with
    the complex object model directly.
    """

    def __init__(
        self,
        owl_file_path: str,
        create_if_not_exists: bool = True,
        serialization: Optional[str] = None,
        readonly: bool = False,
        annotation_property: Optional[str] = None,
        owl_catalog_map: Optional[dict[str, str]] = None,
        use_catalog_if_present: bool = True,
        catalog_default_file_name: str = "catalog-v001.xml",
        catalog_file_path: Optional[str] = None,
        normalize_after_save: bool | None = None,
    ) -> None:
        """
        Initialize the API with a path to an OWL file.

        Args:
            owl_file_path: Path to the OWL file (will be created if it doesn't exist)
            create_if_not_exists: If True, create the file if it doesn't exist
            serialization: Optional serialization format (e.g., "ofn", "rdfxml"). Inferred if not specified
            readonly: If True, changes to the ontology will not be saved to disk
            annotation_property: Default annotation property IRI to use for labels
                                (defaults to rdfs:label if None)
            owl_catalog_map: Optional map of owl ontology IRIs to file paths
        """
        from .config import get_config_manager

        self.owl_file_path = os.path.abspath(owl_file_path)
        self.ontology = None
        self.file_hash = None
        self.observers = []
        self.file_monitor = None
        self.lock = threading.RLock()  # Reentrant lock for thread safety
        self.is_syncing = False  # Flag to prevent recursive sync
        self.import_map = {}

        # Set default annotation property for labels (rdfs:label is the standard default)
        self.annotation_property = (
            annotation_property or "http://www.w3.org/2000/01/rdf-schema#label"
        )

        # Check if we have a configuration for this ontology
        config_manager = get_config_manager()
        ontology_config = config_manager.get_ontology_by_path(self.owl_file_path)

        if ontology_config:
            # Use the configured settings
            self.readonly = ontology_config.readonly if readonly is False else readonly
            self.serialization = serialization or ontology_config.preferred_serialization
            # Update annotation_property from config if it wasn't explicitly provided
            if annotation_property is None and ontology_config.annotation_property is not None:
                self.annotation_property = ontology_config.annotation_property
        else:
            # Use the provided settings
            self.readonly = readonly
            self.serialization = serialization

        # If serialization is still None, try to infer it
        if not self.serialization and owl_file_path.endswith(".owl"):
            # A suffix `.owl` can be any format;
            # allow for the obo convention of using `.owl` for functional syntax for edit files
            # sniff file if it exists:
            with self.lock:
                if os.path.exists(owl_file_path):
                    with open(owl_file_path) as f:
                        first_line = f.readline().strip()
                        if first_line.startswith(("Prefix(", "Ontology(")):
                            self.serialization = "ofn"
                        else:
                            self.serialization = "owl"

        # If we have config metadata, prepare to add it
        self.pending_metadata_axioms = []
        if ontology_config and ontology_config.metadata_axioms:
            self.pending_metadata_axioms = ontology_config.metadata_axioms

        # Try to load the ontology
        self.load_ontology(create_if_not_exists=create_if_not_exists)

        if not owl_catalog_map and use_catalog_if_present:
            if catalog_file_path:
                owl_catalog_map = read_catalog(catalog_file_path)
            else:
                catalog_file_path = Path(self.owl_file_path).parent / catalog_default_file_name
                if catalog_file_path.exists():
                    owl_catalog_map = read_catalog(catalog_file_path)
        self._resolve_imports(owl_catalog_map)

        # Add any configured metadata axioms
        if self.pending_metadata_axioms:
            for axiom_str in self.pending_metadata_axioms:
                try:
                    self.add_axiom(axiom_str, bypass_readonly=True)
                except Exception as e:
                    logger.warning(f"Failed to add metadata axiom: {axiom_str}. Error: {e}")
            self.pending_metadata_axioms = []

        if normalize_after_save is None:
            path_to_makefile = Path(self.owl_file_path).parent / "Makefile"
            if path_to_makefile.exists():
                # read the makefile and look for a rule that normalizes the ontology
                with open(path_to_makefile) as f:
                    for line in f:
                        if line.startswith(MAKEFILE_NORMALIZE_RULE):
                            normalize_after_save = True
                            break

        self.normalize_after_save = normalize_after_save

        # Set up file monitoring
        self._setup_file_monitoring()

    def _resolve_imports(self, owl_catalog_map: Optional[dict[str, str]]) -> None:
        """
        Resolve imports in the ontology.
        """
        if not owl_catalog_map:
            return None

        ontology_map = {}
        ontologies = [self.ontology]
        while ontologies:
            ontology = ontologies.pop(0)
            ontology_iri = str(ontology.get_iri())
            if ontology_iri in ontology_map:
                continue
            ontology_map[ontology_iri] = ontology
            axioms_about_ontology = ontology.get_axioms_for_iri(str(ontology_iri))
            if len(axioms_about_ontology) == 0:
                # TODO: find a more efficient way to do this
                axioms_about_ontology = [c.component for c in ontology.get_components()]
                assert len(axioms_about_ontology) > 0, (
                    "Should have at least one axiom about the ontology"
                )
            import_iris = [
                str(axiom.first) for axiom in axioms_about_ontology if isinstance(axiom, Import)
            ]
            if len(import_iris) == 0:
                continue
            for import_iri in import_iris:
                if import_iri in owl_catalog_map:
                    local_path = owl_catalog_map[import_iri]
                    ontologies.append(
                        pyhornedowl.open_ontology(
                            local_path, serialization=guess_serialization(local_path)
                        )
                    )
                else:
                    ontologies.append(pyhornedowl.open_ontology(str(import_iri)))
        self.import_map = ontology_map
        return ontology_map

    def load_ontology(self, create_if_not_exists: bool = True) -> None:
        """
        Load the ontology from the OWL file or create a new one if it doesn't exist.

        Args:
            create_if_not_exists: If True, create the file if it doesn't exist
        """
        with self.lock:
            if os.path.exists(self.owl_file_path):
                logger.info(f"Loading ontology from {self.owl_file_path}")
                self.ontology = pyhornedowl.open_ontology(
                    self.owl_file_path, serialization=self.serialization
                )
            else:
                if not create_if_not_exists:
                    msg = f"OWL file does not exist: {self.owl_file_path}"
                    raise FileNotFoundError(msg)
                logger.info(f"Creating new ontology at {self.owl_file_path}")
                # Create a minimal ontology file
                os.makedirs(os.path.dirname(os.path.abspath(self.owl_file_path)), exist_ok=True)
                self.ontology = pyhornedowl.PyIndexedOntology()
                self.save_ontology()

            # Set default prefixes
            self.ontology.prefix_mapping.add_default_prefix_names()
            self.file_hash = self._calculate_file_hash()

    def save_ontology(self) -> None:
        """
        Save the ontology to the OWL file.

        If the ontology is marked as readonly, the save operation is skipped.
        """
        with self.lock:
            if self.readonly:
                logger.debug(f"Ontology {self.owl_file_path} is readonly, skipping save")
                return

            if self.ontology is not None:
                logger.debug(f"Saving ontology to {self.owl_file_path}")
                if self.normalize_after_save:
                    # create a backup of the ontology
                    backup_file_path = self.owl_file_path + ".bak"
                    shutil.copy(self.owl_file_path, backup_file_path)
                self.ontology.save_to_file(self.owl_file_path, serialization=self.serialization)
                if self.normalize_after_save:
                    # See: https://github.com/phillord/horned-owl/issues/145
                    # this workaround is needed because horned-owl does not normalize
                    # the ontology using the same specification as the java owlapi.
                    os.chdir(Path(self.owl_file_path).parent)
                    try:
                        subprocess.run(["make", MAKEFILE_NORMALIZE_RULE], check=True)
                    except subprocess.CalledProcessError as e:
                        # restore the backup
                        logger.exception(
                            f"Error normalizing ontology: {e}, restoring backup from {backup_file_path}"
                        )
                        shutil.copy(backup_file_path, self.owl_file_path)
                        raise
                self.file_hash = self._calculate_file_hash()

    def add_axiom(self, axiom_str: str, bypass_readonly: bool = False) -> bool:
        """
        Add an axiom to the ontology using OWL functional syntax.

        Args:
            axiom_str: String representation of the axiom in OWL functional syntax
                       e.g., "SubClassOf(:Dog :Animal)"
            bypass_readonly: If True, the axiom will be added even if the ontology is readonly
                           Useful for adding metadata axioms from config

        Returns:
            bool: True if the axiom was added successfully, False otherwise
        """
        with self.lock:
            if self.readonly and not bypass_readonly:
                logger.warning(f"Cannot add axiom to readonly ontology: {self.owl_file_path}")
                return False

            # Parse the axiom string into a py-horned-owl axiom object
            components = parse_axiom_string(axiom_str, self.ontology)

            # Add the axiom to the ontology
            for c in components:
                self.ontology.add_component(c.component, c.ann)

            # Save only if not readonly or explicitly bypassing readonly
            if not self.readonly or bypass_readonly:
                self.save_ontology()

            self._notify_observers("axiom_added", axiom_str=axiom_str)
            return True

    def remove_axiom(self, axiom_str: str, bypass_readonly: bool = False) -> bool:
        """
        Remove an axiom from the ontology using OWL functional syntax.

        Args:
            axiom_str: String representation of the axiom in OWL functional syntax
            bypass_readonly: If True, the axiom will be removed even if the ontology is readonly

        Returns:
            bool: True if the axiom was removed successfully, False otherwise
        """
        with self.lock:
            if self.readonly and not bypass_readonly:
                logger.warning(f"Cannot remove axiom from readonly ontology: {self.owl_file_path}")
                return False

            components = parse_axiom_string(axiom_str, self.ontology)
            for c in components:
                # Remove the axiom from the ontology
                self.ontology.remove_component(c.component)

            # Save only if not readonly or explicitly bypassing readonly
            if not self.readonly or bypass_readonly:
                self.save_ontology()

            self._notify_observers("axiom_removed", axiom_str=axiom_str)
            return True

    def _compile_regex_pattern(self, pattern: Optional[str]) -> Optional[re.Pattern]:
        """Compile a regex pattern if provided."""
        if not pattern:
            return None
        try:
            return re.compile(pattern)
        except re.error as e:
            msg = f"Invalid regex pattern '{pattern}': {e}"
            raise re.error(msg) from e

    def _matches_criteria(
        self, axiom_str: str, axiom_type: Optional[str], regex_pattern: Optional[re.Pattern]
    ) -> bool:
        """Check if an axiom matches the given criteria."""
        if axiom_type and not axiom_str.startswith(axiom_type + "("):
            return False
        return not (regex_pattern and not regex_pattern.search(axiom_str))

    def _add_labels_to_axiom(self, axiom_str: str, annotation_property: Optional[str]) -> str:
        """Add human-readable labels to an axiom string."""
        found_labels = []
        start_idx = 0

        while True:
            # Find the next IRI (with prefix like ex:Something)
            colon_idx = axiom_str.find(":", start_idx)
            if colon_idx == -1:
                break

            # We need to check if this is an actual CURIE and not just a colon
            prefix_start = colon_idx
            for i in range(colon_idx - 1, -1, -1):
                if axiom_str[i] in " (,":
                    prefix_start = i + 1
                    break

            if prefix_start < colon_idx:  # We have a potential prefix
                # Find the end of the IRI
                end_idx = len(axiom_str)
                for char in " ),":
                    pos = axiom_str.find(char, colon_idx + 1)
                    if pos != -1 and pos < end_idx:
                        end_idx = pos

                if end_idx > colon_idx + 1:  # We have an actual IRI
                    iri = axiom_str[prefix_start:end_idx]

                    # Get labels for this IRI
                    try:
                        labels = self.get_labels_for_iri(iri, annotation_property)
                        if labels:
                            found_labels.append(f'{iri} = "{labels[0]}"')
                    except Exception as e:
                        logger.debug(f"Error getting label for IRI {iri}: {e}")

            # Move to the next position
            start_idx = colon_idx + 1

        # Add all found labels to the axiom string
        if found_labels:
            return f"{axiom_str} ## {'; '.join(found_labels)}"
        return axiom_str

    def find_axioms(
        self,
        pattern: Optional[str],
        axiom_type: Optional[str] = None,
        include_labels: bool = False,
        annotation_property: Optional[str] = None,
        include_imports: bool = True,
    ) -> list[str]:
        """
        Find axioms matching a regex pattern in the ontology.

        Args:
            pattern: A regex pattern to match against axiom strings
                     (supports full Python regex syntax, e.g., r"SubClassOf.*:Animal")
            axiom_type: Optional type of axiom to filter by (e.g., "ClassAssertion")
            include_labels: If True, add human-readable labels after '##' in the output
            annotation_property: Optional annotation property IRI to use for labels
                                (defaults to the instance's annotation_property if None)
            include_imports: If True, include axioms from imported ontologies

        Returns:
            List of matching axiom strings, optionally with human-readable labels

        Raises:
            re.error: If the regex pattern is invalid
        """
        with self.lock:
            axioms = self.get_all_axiom_strings(
                include_labels=False, include_imports=include_imports
            )
            regex_pattern = self._compile_regex_pattern(pattern)

            matching_axioms = [
                str(axiom)
                for axiom in axioms
                if self._matches_criteria(str(axiom), axiom_type, regex_pattern)
            ]

            if include_labels and matching_axioms:
                return [
                    self._add_labels_to_axiom(axiom_str, annotation_property)
                    for axiom_str in matching_axioms
                ]

            return matching_axioms

    def get_all_axiom_strings(
        self,
        include_labels: bool = False,
        annotation_property: Optional[str] = None,
        include_imports: bool = True,
    ) -> list[str]:
        """
        Get all axioms in the ontology as strings.

        Args:
            include_labels: If True, add human-readable labels after '##' in the output
            annotation_property: Optional annotation property IRI to use for labels
                                (defaults to the instance's annotation_property if None)
            include_imports: If True, include axioms from imported ontologies

        Returns:
            List of all axiom strings, optionally with human-readable labels
        """

        with self.lock:
            if include_imports and self.import_map:
                components = []
                for ontology in self.import_map.values():
                    components.extend(ontology.get_axioms())
            else:
                components = self.ontology.get_axioms()
            axiom_strings = serialize_axioms(components, self.ontology)

            if not include_labels:
                return axiom_strings

            return [
                self._add_labels_to_axiom(axiom_str, annotation_property)
                for axiom_str in axiom_strings
            ]

    def add_prefix(self, prefix: str, uri: str, bypass_readonly: bool = False) -> bool:
        """
        Add a prefix mapping to the ontology.

        Args:
            prefix: The prefix string (e.g., "ex:")
            uri: The URI the prefix maps to (e.g., "http://example.org/")
            bypass_readonly: If True, the prefix will be added even if the ontology is readonly

        Returns:
            bool: True if successful, False otherwise
        """
        with self.lock:
            if self.readonly and not bypass_readonly:
                logger.warning(f"Cannot add prefix to readonly ontology: {self.owl_file_path}")
                return False

            self.ontology.add_prefix_mapping(prefix, uri)

            # Save only if not readonly or explicitly bypassing readonly
            if not self.readonly or bypass_readonly:
                self.save_ontology()

            self._notify_observers("prefix_added", prefix=prefix, uri=uri)
            return True

    def ontology_annotations(self) -> list[str]:
        """
        Get all ontology annotations.

        Returns:
            List of ontology annotations
        """
        with self.lock:
            oas = [
                a
                for a in self.ontology.get_components()
                if isinstance(a.component, OntologyAnnotation)
            ]
            return serialize_axioms(oas, self.ontology)

    def register_in_config(
        self,
        name: Optional[str] = None,
        readonly: Optional[bool] = None,
        description: Optional[str] = None,
        preferred_serialization: Optional[str] = None,
        annotation_property: Optional[str] = None,
    ) -> str:
        """
        Register the current ontology in the configuration system.

        Args:
            name: Optional name for the ontology (defaults to filename stem)
            readonly: Whether the ontology should be read-only (defaults to current setting)
            description: Optional description for the ontology
            preferred_serialization: Optional preferred serialization format (defaults to current setting)
            annotation_property: Optional annotation property IRI for labels (defaults to current setting)

        Returns:
            str: Name of the registered ontology
        """
        from .config import get_config_manager

        # Get or create a config manager
        config_manager = get_config_manager()

        # If name is not provided, use the filename stem (without extension)
        if name is None:
            name = Path(self.owl_file_path).stem

        # Extract metadata axioms from the ontology
        metadata_axioms = self.ontology_annotations()

        # Use current values as defaults if not explicitly provided
        readonly_value = self.readonly if readonly is None else readonly
        serialization_value = (
            self.serialization if preferred_serialization is None else preferred_serialization
        )
        annotation_property_value = (
            self.annotation_property if annotation_property is None else annotation_property
        )

        # Register in configuration
        config_manager.add_ontology(
            name=name,
            path=self.owl_file_path,
            metadata_axioms=metadata_axioms,
            readonly=readonly_value,
            description=description,
            preferred_serialization=serialization_value,
            annotation_property=annotation_property_value,
        )

        return name

    def sync_with_file(self) -> None:
        """
        Synchronize the in-memory ontology with the file on disk.
        """
        if self.is_syncing:
            return  # Prevent recursive syncing

        with self.lock:
            try:
                self.is_syncing = True
                current_hash = self._calculate_file_hash()

                if current_hash != self.file_hash:
                    logger.info("External changes detected, reloading ontology")
                    self.load_ontology()
                    self._notify_observers("file_changed")
            finally:
                self.is_syncing = False

    def add_observer(self, callback: Callable) -> None:
        """
        Add an observer to be notified of ontology changes.

        Args:
            callback: Callback function to be called on changes
        """
        if callback not in self.observers:
            self.observers.append(callback)

    def remove_observer(self, callback: Callable) -> None:
        """
        Remove an observer.

        Args:
            callback: Callback function to remove
        """
        if callback in self.observers:
            self.observers.remove(callback)

    def get_labels_for_iri(
        self, iri: str, annotation_property: Optional[str] = None, include_imports: bool = True
    ) -> list[str]:
        """
        Get all labels for a given IRI using a specified annotation property.

        Args:
            iri: The IRI to get labels for (as a string)
            annotation_property: Optional annotation property IRI to use for labels
                                (defaults to the instance's annotation_property if None)
            include_imports: If True, include labels from imported ontologies

        Returns:
            List of label strings
        """
        with self.lock:
            # Use the instance's annotation_property field as default
            label_property = annotation_property or self.annotation_property

            # Check if iri is a CURIE (e.g. "ex:Person") and handle appropriately
            if ":" in iri and not iri.startswith(("http://", "https://")):
                # This is a prefixed IRI, we need to get the full IRI
                prefix, local = iri.split(":", 1)
                # Get all prefixes to find the right one
                prefixes = self.ontology.prefix_mapping
                if prefix in prefixes:
                    full_iri = f"{prefixes[prefix]}{local}"
                else:
                    logger.warning(f"Prefix '{prefix}' not found in ontology")
                    return []
            else:
                # Already a full IRI
                full_iri = iri

            # Get all annotations for this IRI with the specified property
            labels = self.ontology.get_annotations(full_iri, label_property)
            if include_imports and self.import_map:
                labels = []
                for ontology in self.import_map.values():
                    labels.extend(ontology.get_annotations(full_iri, label_property))
            else:
                labels = self.ontology.get_annotations(full_iri, label_property)

            return labels

    def stop(self) -> None:
        """
        Stop the API and release resources.
        """
        logger.info("Stopping SimpleOwlAPI")
        if self.file_monitor is not None:
            self.file_monitor.stop()
            self.file_monitor.join()

    def _calculate_file_hash(self) -> str:
        """
        Calculate a hash of the OWL file contents.

        Returns:
            Hash of the file contents or empty string if file doesn't exist
        """
        if not os.path.exists(self.owl_file_path):
            return ""

        with open(self.owl_file_path, "rb") as f:
            return hashlib.md5(f.read()).hexdigest()

    def _notify_observers(self, event_type: str, **kwargs) -> None:
        """
        Notify all registered observers of an event.

        Args:
            event_type: Type of event that occurred
            **kwargs: Additional event data
        """
        for observer in self.observers:
            self._call_observer_safely(observer, event_type, **kwargs)

    def _call_observer_safely(self, observer, event_type, **kwargs):
        """Call an observer function with error handling.

        Args:
            observer: Observer function to call
            event_type: Type of event that occurred
            **kwargs: Additional event data
        """
        try:
            observer(event_type=event_type, api=self, **kwargs)
        except Exception:
            logger.exception("Error in observer callback")

    def _setup_file_monitoring(self) -> None:
        """
        Set up monitoring for changes to the OWL file on disk.
        """

        class OWLFileHandler(FileSystemEventHandler):
            def __init__(self, api):
                self.api = api

            def on_modified(self, event):
                if (
                    not event.is_directory
                    and os.path.abspath(event.src_path) == self.api.owl_file_path
                ):
                    logger.debug(f"File change detected: {event.src_path}")
                    self.api.sync_with_file()

        directory = os.path.dirname(self.owl_file_path)
        observer = Observer()
        event_handler = OWLFileHandler(self)
        observer.schedule(event_handler, directory, recursive=False)
        observer.start()
        self.file_monitor = observer
