"""Build the Visual Studio extension for KC (language client + TextMate grammar) without the VSSDK.

    python -X utf8 -B editors/vs/build_vsix.py [--vs <VS install dir>] [--python <python.exe>] [--check]

1. compiles KcLanguageClient.cs with the csc of the Visual Studio installation against its own assemblies and the
   .NET Framework 4.7.2 reference assemblies;
2. writes extension.vsixmanifest, [Content_Types].xml, manifest.json, catalog.json, kc.pkgdef (the grammar
   registration) and kclsp.json (the Python and the x3kc checkout the client starts the server with);
3. zips everything into editors/vs/dist/x3kc-kc.vsix.
--check then loads the DLL with CheckClient.exe (.NET Framework, as devenv) with the VS folders as probing path,
resolves every type, prints the MEF exports, creates the client object, and compares the package with an installed
extension.

kclsp.json holds this machine's paths: the .vsix is a local build, not for the repository (build/ and dist/ are
ignored).
"""
import argparse
import glob
import hashlib
import json
import os
import subprocess
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
GRAMMAR = os.path.join(ROOT, 'editors', 'kc-textmate')
BUILD = os.path.join(HERE, 'build')
DIST = os.path.join(HERE, 'dist')
VSIX = os.path.join(DIST, 'x3kc-kc.vsix')
ID = 'x3kc.KcLanguage'
VERSION = '0.1.0'
DLL = 'X3kc.KcClient.dll'
BS = chr(92)    # a backslash

REFS = [
    'Common7/IDE/CommonExtensions/Microsoft/LanguageServer/Microsoft.VisualStudio.LanguageServer.Client.dll',
    'Common7/IDE/CommonExtensions/Microsoft/Editor/Microsoft.VisualStudio.CoreUtility.dll',
    # the copy devenv.exe.config binds Microsoft.VisualStudio.Threading to (the PrivateAssemblies one is 15.8)
    'Common7/IDE/PublicAssemblies/Microsoft.VisualStudio.Threading.17.x/Microsoft.VisualStudio.Threading.dll',
    'Common7/IDE/PublicAssemblies/StreamJsonRpc.2.x/StreamJsonRpc.dll',
]
FX = ['mscorlib.dll', 'System.dll', 'System.Core.dll', 'System.ComponentModel.Composition.dll',
      'System.Runtime.Serialization.dll', 'System.Xml.dll', 'Facades/netstandard.dll', 'Facades/System.Runtime.dll',
      'Facades/System.Threading.Tasks.dll']
FX_DIR = 'C:/Program Files (x86)/Reference Assemblies/Microsoft/Framework/.NETFramework/v4.7.2'

MANIFEST = '''<?xml version="1.0" encoding="utf-8"?>
<PackageManifest Version="2.0.0" xmlns="http://schemas.microsoft.com/developer/vsx-schema/2011">
  <Metadata>
    <Identity Id="{id}" Version="{ver}" Language="en-US" Publisher="x3kc" />
    <DisplayName>KC language (x3kc)</DisplayName>
    <Description xml:space="preserve">KC script sources of X3 (x3kc): highlighting, diagnostics, outline and go to definition through the x3kc language server.</Description>
  </Metadata>
  <Installation AllUsers="false">
    <InstallationTarget Id="Microsoft.VisualStudio.Community" Version="[17.0,19.0)">
      <ProductArchitecture>amd64</ProductArchitecture>
    </InstallationTarget>
  </Installation>
  <Dependencies>
    <Dependency Id="Microsoft.Framework.NDP" DisplayName="Microsoft .NET Framework" Version="[4.7.2,)" />
  </Dependencies>
  <Prerequisites>
    <Prerequisite Id="Microsoft.VisualStudio.Component.CoreEditor" Version="[17.0,19.0)" DisplayName="Visual Studio core editor" />
  </Prerequisites>
  <Assets>
    <Asset Type="Microsoft.VisualStudio.MefComponent" Path="{dll}" />
    <Asset Type="Microsoft.VisualStudio.VsPackage" Path="kc.pkgdef" />
  </Assets>
</PackageManifest>
'''

CONTENT_TYPES = '''<?xml version="1.0" encoding="utf-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="vsixmanifest" ContentType="text/xml" />
  <Default Extension="dll" ContentType="application/octet-stream" />
  <Default Extension="pkgdef" ContentType="text/plain" />
  <Default Extension="json" ContentType="application/json" />
</Types>
'''

# the grammar for the content type, in the registry layout of TMLanguage.pkgdef in the VS TextMate folder
PKGDEF = '\n'.join([
    '[$RootKey$/TextMate/Repositories]',
    '"kc"="$PackageFolder$/Grammars"',
    '',
    '[$RootKey$/TextMate/LanguageConfiguration/GrammarMapping]',
    '"source.kc"="$PackageFolder$/Grammars/language-configuration.json"',
    '',
    '[$RootKey$/TextMate/LanguageConfiguration/ContentTypeMapping]',
    '"kc"="$PackageFolder$/Grammars/language-configuration.json"',
    '']).replace('/', BS)


def find_vs(arg):
    if arg:
        return arg
    c = sorted(glob.glob('C:/Program Files/Microsoft Visual Studio/*/*/Common7/IDE/devenv.exe'))
    if not c:
        sys.exit('no Visual Studio installation found; pass --vs')
    return os.path.dirname(os.path.dirname(os.path.dirname(c[-1])))


def win(p):
    return p.replace('/', BS)


def compile_dll(vs):
    csc = os.path.join(vs, 'MSBuild/Current/Bin/Roslyn/csc.exe')
    refs = [os.path.join(vs, r) for r in REFS] + [os.path.join(FX_DIR, r) for r in FX]
    missing = [r for r in refs + [csc] if not os.path.isfile(r)]
    if missing:
        sys.exit('missing: ' + ', '.join(missing))
    os.makedirs(BUILD, exist_ok=True)
    out = os.path.join(BUILD, DLL)
    cmd = [csc, '/nologo', '/utf8output', '/noconfig', '/nostdlib', '/target:library', '/langversion:latest',
           '/optimize+', '/deterministic', '/nowarn:CS0649,CS0067,CS1701', '/out:' + out] + \
          ['/r:' + r for r in refs] + [os.path.join(HERE, 'KcLanguageClient.cs')]
    r = subprocess.run(cmd, capture_output=True, encoding='utf-8', errors='replace')
    sys.stdout.write(r.stdout + r.stderr)
    if r.returncode:
        sys.exit('csc failed (%d)' % r.returncode)
    return out


def files(dll, python):
    with open(os.path.join(GRAMMAR, 'syntaxes', 'kc.tmLanguage.json'), 'rb') as f:
        grammar = f.read()
    with open(os.path.join(GRAMMAR, 'language-configuration.json'), 'rb') as f:
        langconf = f.read()
    with open(dll, 'rb') as f:
        dllb = f.read()
    settings = json.dumps({'python': python.replace(BS, '/'), 'x3kc_root': ROOT.replace(BS, '/'), 'log': None},
                          indent=2).encode('utf-8')
    out = {
        'extension.vsixmanifest': MANIFEST.format(id=ID, ver=VERSION, dll=DLL).encode('utf-8'),
        DLL: dllb,
        'kc.pkgdef': PKGDEF.encode('utf-8'),
        'kclsp.json': settings,
        'Grammars/kc.tmLanguage.json': grammar,
        'Grammars/language-configuration.json': langconf,
    }
    size = sum(len(b) for b in out.values())
    ext_dir = BS.join(['[installdir]', 'Common7', 'IDE', 'Extensions', 'x3kc-kc'])
    out['manifest.json'] = json.dumps({
        'id': ID, 'version': VERSION, 'type': 'Vsix', 'vsixId': ID, 'extensionDir': ext_dir,
        'files': [{'fileName': '/' + n, 'sha256': hashlib.sha256(b).hexdigest().upper()
                   if n == 'extension.vsixmanifest' else None} for n, b in sorted(out.items())],
        'installSizes': {'targetDrive': size},
        'dependencies': {'Microsoft.VisualStudio.Component.CoreEditor': '[17.0,19.0)'},
    }, separators=(',', ':')).encode('utf-8')
    out['catalog.json'] = json.dumps({
        'manifestVersion': '1.1',
        'info': {'id': '%s,version=%s' % (ID, VERSION), 'manifestType': 'Extension'},
        'packages': [
            {'id': 'Component.' + ID, 'version': VERSION, 'type': 'Component', 'extension': True,
             'dependencies': {ID: VERSION, 'Microsoft.VisualStudio.Component.CoreEditor': '[17.0,19.0)'}},
            {'id': ID, 'version': VERSION, 'type': 'Vsix',
             'payloads': [{'fileName': os.path.basename(VSIX), 'size': 0}],
             'vsixId': ID, 'extensionDir': ext_dir, 'installSizes': {'targetDrive': size}},
        ],
    }, separators=(',', ':')).encode('utf-8')
    out['[Content_Types].xml'] = CONTENT_TYPES.encode('utf-8')
    return out


def write_vsix(out):
    os.makedirs(DIST, exist_ok=True)
    tmp = VSIX + '.tmp'
    with zipfile.ZipFile(tmp, 'w', zipfile.ZIP_DEFLATED) as z:
        for n in ['[Content_Types].xml'] + sorted(k for k in out if k != '[Content_Types].xml'):
            z.writestr(n, out[n])
    os.replace(tmp, VSIX)


def check(vs, dll):
    csc = os.path.join(vs, 'MSBuild/Current/Bin/Roslyn/csc.exe')
    exe = os.path.join(BUILD, 'CheckClient.exe')
    r = subprocess.run([csc, '/nologo', '/utf8output', '/target:exe', '/out:' + exe,
                        os.path.join(HERE, 'CheckClient.cs')], capture_output=True, encoding='utf-8', errors='replace')
    sys.stdout.write(r.stdout + r.stderr)
    if r.returncode:
        return False
    r = subprocess.run([exe, win(vs), win(dll)], capture_output=True, encoding='utf-8', errors='replace')
    sys.stdout.write(r.stdout + r.stderr)
    ok = r.returncode == 0 and 'FAILED' not in r.stdout and 'ILanguageClient' in r.stdout
    # the package against an installed extension's layout
    with zipfile.ZipFile(VSIX) as z:
        names = z.namelist()
        man = z.read('extension.vsixmanifest').decode('utf-8')
        mj = json.loads(z.read('manifest.json'))
        cat = json.loads(z.read('catalog.json'))
        pk = z.read('kc.pkgdef').decode('utf-8')
    ref = os.path.join(vs, 'Common7/IDE/CommonExtensions/Microsoft/TextMate')
    if os.path.isdir(ref):
        with open(os.path.join(ref, 'manifest.json'), encoding='utf-8-sig') as f:
            rj = json.load(f)
        with open(os.path.join(ref, 'TMLanguage.pkgdef'), encoding='utf-8-sig') as f:
            rp = f.read()
        print('manifest.json keys: ours %s, installed TextMate %s' % (sorted(mj), sorted(rj)))
        for key in ('GrammarMapping', 'ContentTypeMapping'):
            k = BS.join(['TextMate', 'LanguageConfiguration', key])
            print('pkgdef key %s: in installed TMLanguage.pkgdef %s, in ours %s' % (k, k in rp, k in pk))
    listed = {f['fileName'].lstrip('/') for f in mj['files']}
    print('vsix files:', names)
    print('files missing from manifest.json:', sorted(set(names) - listed - {'manifest.json', 'catalog.json',
                                                                             '[Content_Types].xml'}) or 'none')
    for a in ('MefComponent" Path="%s"' % DLL, 'VsPackage" Path="kc.pkgdef"'):
        print('asset %s: %s' % (a.split('"')[0], 'ok' if a in man else 'MISSING'))
        ok = ok and a in man
    print('catalog packages:', [p['id'] for p in cat['packages']])
    print('CHECK', 'OK' if ok else 'FAILED')
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--vs')
    ap.add_argument('--python', default=sys.executable)
    ap.add_argument('--check', action='store_true')
    a = ap.parse_args()
    vs = find_vs(a.vs)
    dll = compile_dll(vs)
    write_vsix(files(dll, a.python))
    print('wrote', VSIX, os.path.getsize(VSIX), 'bytes')
    if a.check and not check(vs, dll):
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
