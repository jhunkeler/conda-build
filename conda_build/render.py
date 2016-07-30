# (c) Continuum Analytics, Inc. / http://continuum.io
# All Rights Reserved
#
# conda is distributed under the terms of the BSD 3-clause license.
# Consult LICENSE.txt or http://opensource.org/licenses/BSD-3-Clause.

from __future__ import absolute_import, division, print_function

from locale import getpreferredencoding
import shutil
import sys
import tarfile
import tempfile
import os
from os.path import isdir, isfile, abspath
import subprocess

import yaml

from conda.compat import PY3
from conda.install import move_to_trash, rm_rf
from conda.lock import Locked

from conda_build import exceptions, utils
from conda_build.config import config
from conda_build.metadata import MetaData
import conda_build.source as source
from conda_build.completers import all_versions, conda_version


def set_language_env_vars(args, parser, execute=None):
    """Given args passed into conda command, set language env vars"""
    for lang in all_versions:
        versions = getattr(args, lang)
        if not versions:
            continue
        if versions == ['all']:
            if all_versions[lang]:
                versions = all_versions[lang]
            else:
                parser.error("'all' is not supported for --%s" % lang)
        if len(versions) > 1:
            for ver in versions[:]:
                setattr(args, lang, [str(ver)])
                if execute:
                    execute(args, parser)
                # This is necessary to make all combinations build.
                setattr(args, lang, versions)
            return
        else:
            version = versions[0]
            if lang in ('python', 'numpy'):
                version = int(version.replace('.', ''))
            setattr(config, conda_version[lang], version)
        if not len(str(version)) in (2, 3) and lang in ['python', 'numpy']:
            if all_versions[lang]:
                raise RuntimeError("%s must be major.minor, like %s, not %s" %
                    (conda_version[lang], all_versions[lang][-1] / 10, version))
            else:
                raise RuntimeError("%s must be major.minor, not %s" %
                    (conda_version[lang], version))

    # Using --python, --numpy etc. is equivalent to using CONDA_PY, CONDA_NPY, etc.
    # Auto-set those env variables
    for var in conda_version.values():
        if hasattr(config, var) and getattr(config, var):
            # Set the env variable.
            os.environ[var] = str(getattr(config, var))


def bldpkg_path(m):
    '''
    Returns path to built package's tarball given its ``Metadata``.
    '''
    return os.path.join(config.bldpkgs_dir, '%s.tar.bz2' % m.dist())


def parse_or_try_download(metadata, no_download_source, verbose,
                          force_download=False, dirty=False):

    need_reparse_in_env = False
    if (force_download or (not no_download_source and (metadata.uses_vcs_in_meta() or
                                                       metadata.uses_setuptools_in_meta()))):
        if dirty:
            need_source_download = False

        # this try/catch is for when the tool to download source is actually in
        #    meta.yaml, and not previously installed in builder env.
        try:
            if not dirty:
                source.provide(metadata.path, metadata.get_section('source'),
                               verbose=verbose)
                need_source_download = False
            try:
                metadata.parse_again(permit_undefined_jinja=False)
            except exceptions.UnableToParseMissingSetuptoolsDependencies:
                need_reparse_in_env = True
        except subprocess.CalledProcessError as error:
            print("Warning: failed to download source.  If building, will try "
                "again after downloading recipe dependencies.")
            print("Error was: ")
            print(error)
            need_source_download = True
    elif not metadata.get_section('source'):
        need_source_download = False
    else:
        # we have not downloaded source in the render phase.  Download it in
        #     the build phase
        need_source_download = True
    try:
        metadata.parse_again(permit_undefined_jinja=False)
    except exceptions.UnableToParseMissingSetuptoolsDependencies:
        need_reparse_in_env = True
    return metadata, need_source_download, need_reparse_in_env


def reparse(metadata):
    """Some things need to be parsed again after the build environment has been created
    and activated."""
    sys.path.insert(0, config.build_prefix)
    sys.path.insert(0, utils.get_site_packages(config.build_prefix))
    metadata.parse_again(permit_undefined_jinja=False)


def render_recipe(recipe_path, no_download_source, verbose, dirty=False):
    with Locked(config.croot):
        if not dirty:
            if sys.platform == 'win32':
                if isdir(source.WORK_DIR):
                    move_to_trash(source.WORK_DIR, '')
            else:
                rm_rf(source.WORK_DIR)

            assert not isdir(source.WORK_DIR), ("Failed to clean work directory.  Please close open"
                                        " programs/terminals/folders and try again.")

        arg = recipe_path
        # Don't use byte literals for paths in Python 2
        if not PY3:
            arg = arg.decode(getpreferredencoding() or 'utf-8')
        if isfile(arg):
            if arg.endswith(('.tar', '.tar.gz', '.tgz', '.tar.bz2')):
                recipe_dir = tempfile.mkdtemp()
                t = tarfile.open(arg, 'r:*')
                t.extractall(path=recipe_dir)
                t.close()
                need_cleanup = True
            else:
                print("Ignoring non-recipe: %s" % arg)
                return
        else:
            recipe_dir = abspath(arg)
            need_cleanup = False

        if not isdir(recipe_dir):
            sys.exit("Error: no such directory: %s" % recipe_dir)

        try:
            m = MetaData(recipe_dir)
        except exceptions.YamlParsingError as e:
            sys.stderr.write(e.error_msg())
            sys.exit(1)

        m, need_download, need_reparse_in_env = parse_or_try_download(m,
                                                            no_download_source=no_download_source,
                                                            verbose=verbose, dirty=dirty)

        if need_cleanup:
            shutil.rmtree(recipe_dir)

    return m, need_download, need_reparse_in_env


# Next bit of stuff is to support YAML output in the order we expect.
# http://stackoverflow.com/a/17310199/1170370
class _MetaYaml(dict):
    fields = ["package", "source", "build", "requirements", "test", "about", "extra"]

    def to_omap(self):
        return [(field, self[field]) for field in _MetaYaml.fields if field in self]


def _represent_omap(dumper, data):
    return dumper.represent_mapping(u'tag:yaml.org,2002:map', data.to_omap())


def _unicode_representer(dumper, uni):
    node = yaml.ScalarNode(tag=u'tag:yaml.org,2002:str', value=uni)
    return node


class _IndentDumper(yaml.Dumper):
    def increase_indent(self, flow=False, indentless=False):
        return super(_IndentDumper, self).increase_indent(flow, False)

yaml.add_representer(_MetaYaml, _represent_omap)
if PY3:
    yaml.add_representer(str, _unicode_representer)
    unicode = None  # silence pyflakes about unicode not existing in py3
else:
    yaml.add_representer(unicode, _unicode_representer)


def output_yaml(metadata, filename=None):
    output = yaml.dump(_MetaYaml(metadata.meta), Dumper=_IndentDumper,
                       default_flow_style=False, indent=4)
    if filename:
        with open(filename, "w") as f:
            f.write(output)
        return("Wrote yaml to %s" % filename)
    else:
        return(output)
