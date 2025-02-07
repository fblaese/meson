# Copyright 2017 The Meson development team

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

#     http://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


import lzma
import gzip
import os
import sys
import shutil
import subprocess
import hashlib
from glob import glob
from mesonbuild.environment import detect_ninja
from mesonbuild.mesonlib import windows_proof_rmtree
from mesonbuild import mlog, build

archive_choices = ['gztar', 'xztar', 'zip']
archive_extension = {'gztar': '.tar.gz',
                     'xztar': '.tar.xz',
                     'zip': '.zip'}

def add_arguments(parser):
    parser.add_argument('--formats', default='xztar',
                        help='Comma separated list of archive types to create.')


def create_hash(fname):
    hashname = fname + '.sha256sum'
    m = hashlib.sha256()
    m.update(open(fname, 'rb').read())
    with open(hashname, 'w') as f:
        f.write('%s %s\n' % (m.hexdigest(), os.path.basename(fname)))


def del_gitfiles(dirname):
    for f in glob(os.path.join(dirname, '.git*')):
        if os.path.isdir(f) and not os.path.islink(f):
            windows_proof_rmtree(f)
        else:
            os.unlink(f)

def process_submodules(dirname):
    module_file = os.path.join(dirname, '.gitmodules')
    if not os.path.exists(module_file):
        return
    subprocess.check_call(['git', 'submodule', 'update', '--init', '--recursive'], cwd=dirname)
    for line in open(module_file):
        line = line.strip()
        if '=' not in line:
            continue
        k, v = line.split('=', 1)
        k = k.strip()
        v = v.strip()
        if k != 'path':
            continue
        del_gitfiles(os.path.join(dirname, v))


def run_dist_scripts(dist_root, dist_scripts):
    assert(os.path.isabs(dist_root))
    env = os.environ.copy()
    env['MESON_DIST_ROOT'] = dist_root
    for d in dist_scripts:
        script = d['exe']
        args = d['args']
        name = ' '.join(script + args)
        print('Running custom dist script {!r}'.format(name))
        try:
            rc = subprocess.call(script + args, env=env)
            if rc != 0:
                sys.exit('Dist script errored out')
        except OSError:
            print('Failed to run dist script {!r}'.format(name))
            sys.exit(1)


def git_have_dirty_index(src_root):
    '''Check whether there are uncommitted changes in git'''
    ret = subprocess.call(['git', '-C', src_root, 'diff-index', '--quiet', 'HEAD'])
    return ret == 1

def create_dist_git(dist_name, archives, src_root, bld_root, dist_sub, dist_scripts):
    if git_have_dirty_index(src_root):
        mlog.warning('Repository has uncommitted changes that will not be included in the dist tarball')
    distdir = os.path.join(dist_sub, dist_name)
    if os.path.exists(distdir):
        shutil.rmtree(distdir)
    os.makedirs(distdir)
    subprocess.check_call(['git', 'clone', '--shared', src_root, distdir])
    process_submodules(distdir)
    del_gitfiles(distdir)
    run_dist_scripts(distdir, dist_scripts)
    output_names = []
    for a in archives:
        compressed_name = distdir + archive_extension[a]
        shutil.make_archive(distdir, a, root_dir=dist_sub, base_dir=dist_name)
        output_names.append(compressed_name)
    shutil.rmtree(distdir)
    return output_names


def hg_have_dirty_index(src_root):
    '''Check whether there are uncommitted changes in hg'''
    out = subprocess.check_output(['hg', '-R', src_root, 'summary'])
    return b'commit: (clean)' not in out

def create_dist_hg(dist_name, archives, src_root, bld_root, dist_sub, dist_scripts):
    if hg_have_dirty_index(src_root):
        mlog.warning('Repository has uncommitted changes that will not be included in the dist tarball')

    os.makedirs(dist_sub, exist_ok=True)
    tarname = os.path.join(dist_sub, dist_name + '.tar')
    xzname = tarname + '.xz'
    gzname = tarname + '.gz'
    zipname = os.path.join(dist_sub, dist_name + '.zip')
    subprocess.check_call(['hg', 'archive', '-R', src_root, '-S', '-t', 'tar', tarname])
    output_names = []
    if dist_scripts:
        mlog.warning('dist scripts are not supported in Mercurial projects')
    if 'xztar' in archives:
        with lzma.open(xzname, 'wb') as xf, open(tarname, 'rb') as tf:
            shutil.copyfileobj(tf, xf)
        output_names.append(xzname)
    if 'gztar' in archives:
        with gzip.open(gzname, 'wb') as zf, open(tarname, 'rb') as tf:
            shutil.copyfileobj(tf, zf)
        output_names.append(gzname)
    os.unlink(tarname)
    if 'zip' in archives:
        subprocess.check_call(['hg', 'archive', '-R', src_root, '-S', '-t', 'zip', zipname])
        output_names.append(zipname)
    return output_names


def check_dist(packagename, meson_command, privdir):
    print('Testing distribution package %s' % packagename)
    unpackdir = os.path.join(privdir, 'dist-unpack')
    builddir = os.path.join(privdir, 'dist-build')
    installdir = os.path.join(privdir, 'dist-install')
    for p in (unpackdir, builddir, installdir):
        if os.path.exists(p):
            shutil.rmtree(p)
        os.mkdir(p)
    ninja_bin = detect_ninja()
    try:
        shutil.unpack_archive(packagename, unpackdir)
        unpacked_files = glob(os.path.join(unpackdir, '*'))
        assert(len(unpacked_files) == 1)
        unpacked_src_dir = unpacked_files[0]
        if subprocess.call(meson_command + ['--backend=ninja', unpacked_src_dir, builddir]) != 0:
            print('Running Meson on distribution package failed')
            return 1
        if subprocess.call([ninja_bin], cwd=builddir) != 0:
            print('Compiling the distribution package failed')
            return 1
        if subprocess.call([ninja_bin, 'test'], cwd=builddir) != 0:
            print('Running unit tests on the distribution package failed')
            return 1
        myenv = os.environ.copy()
        myenv['DESTDIR'] = installdir
        if subprocess.call([ninja_bin, 'install'], cwd=builddir, env=myenv) != 0:
            print('Installing the distribution package failed')
            return 1
    finally:
        shutil.rmtree(unpackdir)
        shutil.rmtree(builddir)
        shutil.rmtree(installdir)
    print('Distribution package %s tested' % packagename)
    return 0

def determine_archives_to_generate(options):
    result = []
    for i in options.formats.split(','):
        if i not in archive_choices:
            sys.exit('Value "{}" not one of permitted values {}.'.format(i, archive_choices))
        result.append(i)
    if len(i) == 0:
        sys.exit('No archive types specified.')
    return result

def run(options):
    b = build.load('.')
    # This import must be load delayed, otherwise it will get the default
    # value of None.
    from mesonbuild.mesonlib import meson_command
    src_root = b.environment.source_dir
    bld_root = b.environment.build_dir
    priv_dir = os.path.join(bld_root, 'meson-private')
    dist_sub = os.path.join(bld_root, 'meson-dist')

    dist_name = b.project_name + '-' + b.project_version

    archives = determine_archives_to_generate(options)

    _git = os.path.join(src_root, '.git')
    if os.path.isdir(_git) or os.path.isfile(_git):
        names = create_dist_git(dist_name, archives, src_root, bld_root, dist_sub, b.dist_scripts)
    elif os.path.isdir(os.path.join(src_root, '.hg')):
        names = create_dist_hg(dist_name, archives, src_root, bld_root, dist_sub, b.dist_scripts)
    else:
        print('Dist currently only works with Git or Mercurial repos')
        return 1
    if names is None:
        return 1
    # Check only one.
    rc = check_dist(names[0], meson_command, priv_dir)
    if rc == 0:
        for name in names:
            create_hash(name)
    return rc
