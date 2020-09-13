import fnmatch
import os
import pathlib

from pydo import *

from .. import config, packages, kernel

env = os.environ.copy()

this_dir = pathlib.Path(__file__).parent

try:
    env['http_proxy'] = os.environ['APT_HTTP_PROXY']
except KeyError:
    print("Don't forget to set up apt-cacher-ng")


def read_excludes(excludefile):
    exclude_data = []
    with excludefile.open() as e:
        for line in e:
            line = line.strip()
            if line.startswith('#'):
                continue
            x = line.split('=')
            if len(x) == 2:
                if x[0] == 'path-exclude':
                    exclude_data.append((x[1][1:], True))
                elif x[0] == 'path-include':
                    exclude_data.append((x[1][1:], False))
    return exclude_data


def test_excludes(path, exclude_data):
    delete = False
    for e in exclude_data:
        if fnmatch.fnmatchcase(str(path), e[0]):
            delete = e[1]
    return delete


def apply_excludes(root, exclude_data):
    for path in root.rglob('*'):
        rpath = path.relative_to(root)
        if test_excludes(rpath, exclude_data):
            try:
                if path.is_dir():
                    os.rmdir(path)
                    #print('delete file', path)
                else:
                    os.remove(path)
                    #print('delete dir', path)
            except OSError:
                #print('delete failed', path)
                pass


multistrap_conf = this_dir / 'multistrap.conf'
multistrap_conf_in = this_dir / 'multistrap.conf.in'
hosts_in = this_dir / 'hosts.in'
hosts = this_dir / 'hosts'
overlay = this_dir / 'overlay'
stage = this_dir / 'stage'
initrd = this_dir / 'initrd'
excludes = this_dir / 'excludes.conf'
cleanup = this_dir / 'cleanup'
chroot = 'proot -b /dev -0 -q qemu-arm -w / -r'
chroot_nobind = 'proot -0 -q qemu-arm -w / -r'
kernel_root_tarballs = [k.root for k in kernel.kernels]
package_tarballs = [p.package['target'] for p in packages.packages.values()]

def package_install_actions():
    for p in packages.packages.values():
        for a in p.package['install']:
            yield a.format(**locals(), **globals())


@command(produces=[multistrap_conf], consumes=[multistrap_conf_in], always=True)
def build_multistrap_conf():
    all_root_debs = sorted(set.union(*(set(p.package['root_debs']) for p in packages.packages.values()), set()))
    multistrap_packages = textwrap(all_root_debs, prefix='packages=')
    subst(multistrap_conf_in, multistrap_conf, {'@PACKAGES@': multistrap_packages})


@command(produces=[hosts], consumes=[hosts_in], always=True)
def build_hosts():
    subst(hosts_in, hosts, {'@HOSTNAME@': config.hostname})


@command(
    produces=[initrd],
    consumes=[
        multistrap_conf,
        hosts,
        *dir_scan(overlay),
        *kernel_root_tarballs,
        *package_tarballs,
        excludes,
    ])
def build():
    call([
        f'rm -rf --one-file-system {stage}/*',

        f'mkdir -p {stage}/etc/apt/trusted.gpg.d/',
        f'gpg --export 82B129927FA3303E > {stage}/etc/apt/trusted.gpg.d/raspberrypi-archive-keyring.gpg',
        f'gpg --export 9165938D90FDDD2E > {stage}/etc/apt/trusted.gpg.d/raspbian-archive-key.gpg',
        f'gpg --export 04EE7237B7D453EC > {stage}/etc/apt/trusted.gpg.d/deb1.gpg',
        f'gpg --export 648ACFD622F3D138 > {stage}/etc/apt/trusted.gpg.d/deb2.gpg',
        f'gpg --export DCC9EFBF77E11517 > {stage}/etc/apt/trusted.gpg.d/deb3.gpg',
        f'/usr/sbin/multistrap -d {stage} -f {multistrap_conf}',
    ], shell=True, env=env)

    script_dir = stage / 'var/lib/dpkg/info'
    for f in script_dir.iterdir():
        if f.suffix == '.preinst':
            script_env = env.copy()
            script_env['DPKG_MAINTSCRIPT_NAME'] = 'preinst'
            script_env['DPKG_MAINTSCRIPT_PACKAGE'] = f.stem
            if f.stem not in ['vpnc']:
                call([
                    f'{chroot} {stage} {f.relative_to(stage)} install',
                ], env=script_env)

    call([
        # don't run makedev
        # we will create device nodes later, after we are done with the system dev
        f'rm -f {stage}/var/lib/dpkg/info/makedev.postinst',

        # work around https://pad.lv/1727874
        f'rm -f {stage}/var/lib/dpkg/info/raspbian-archive-keyring.postinst',
        f'ln -sf /usr/share/keyrings/raspbian-archive-keyring.gpg {stage}/etc/apt/trusted.gpg.d/',

        # work around PAM error
        f'ln -s -f /bin/true {stage}/usr/bin/chfn',

        # configure packages
        f'DEBIAN_FRONTEND=noninteractive DEBCONF_NONINTERACTIVE_SEEN=true \
            LC_ALL=C LANGUAGE=C LANG=C {chroot} {stage} /usr/bin/dpkg --configure --debug=1 -a || true',

        # initialize /etc/fstab
        f'echo proc /proc proc defaults 0 0 > {stage}/etc/fstab',

        # hostname
        f'echo {config.hostname} > {stage}/etc/hostname',

        # hosts
        f'cp {hosts} {stage}/etc/',

        # delete root password
        f'{chroot} {stage} passwd -d root',
    ], shell=True, env=env)

    # remove excluded files that multistrap missed
    apply_excludes(stage, read_excludes(excludes))

    call([
        # install the excludes to the image so they they are applied if user installs something at run time
        f'mkdir -p {stage}/etc/dpkg/dpkg.conf.d/',
        f'cp {excludes} {stage}/etc/dpkg/dpkg.conf.d/',

        # update hwdb after cleaning
        f'{chroot} {stage} udevadm hwdb --update --usr',

        # modules
        *list(f'tar -xf {kr} -C {stage}' for kr in kernel_root_tarballs),

        # packages
        *list(f'tar -xf {pkg} -C {stage}' for pkg in package_tarballs),
        *list(package_install_actions()),

        # overlay
        f'cp -r {overlay}/* {stage}',

        # ldconfig
        f'{chroot} {stage} /sbin/ldconfig -r /',

        # reset default udev persistent-net rule
        f'rm -f {stage}/etc/udev/rules.d/*_persistent-net.rules',

        # time used by timesyncd if no other is available
        f'touch {stage}/var/lib/systemd/clock',

        # mtab
        f'ln -sf /proc/mounts {stage}/etc/mtab',

        # this must be done last. if the fakeroot devices exist on the system,
        # chroot wont be able to read from them, which breaks systemd setup.
        f'cd {stage}/dev && fakeroot /sbin/MAKEDEV std',

        # pack rootfs into initrd
        f'{chroot_nobind} {stage} sh -c "cd / && find * -xdev -not \( \
                  -path host-rootfs -prune \
                  -path run -prune \
                  -path proc -prune \
                  -path sys -prune \
                  -path boot -prune \
               \) | cpio --create -H newc" | xz -C crc32 -9 > {initrd}'

    ], shell=True)


@command()
def clean():
    call([
        f'rm -rf --one-file-system {stage}/* {initrd} {multistrap_conf} {hosts}'
    ])


@command()
def enter():
    call([
        f'{chroot} {stage} /bin/bash'
    ], interactive=True, check=False)
