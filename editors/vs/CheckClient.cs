// build_vsix.py --check: load the client DLL on .NET Framework (as devenv does) with the Visual Studio folders as
// probing path, load every referenced assembly, resolve every type and print the MEF attributes.
// CheckClient.exe <VS install dir> <client dll>
using System;
using System.IO;
using System.Linq;
using System.Reflection;

static class CheckClient
{
    static string[] dirs;

    static int Main(string[] args)
    {
        var vs = args[0];
        dirs = new[] {
            @"Common7\IDE\PublicAssemblies\Microsoft.VisualStudio.Threading.17.x",
            @"Common7\IDE\PublicAssemblies\StreamJsonRpc.2.x",
            @"Common7\IDE\CommonExtensions\Microsoft\LanguageServer",
            @"Common7\IDE\CommonExtensions\Microsoft\Editor",
            @"Common7\IDE\PublicAssemblies", @"Common7\IDE\PrivateAssemblies", @"Common7\IDE",
        }.Select(d => Path.Combine(vs, d)).ToArray();
        AppDomain.CurrentDomain.AssemblyResolve += (s, e) =>
        {
            var n = new AssemblyName(e.Name).Name;
            if (n.EndsWith(".resources")) return null;
            foreach (var d in dirs)
            {
                var p = Path.Combine(d, n + ".dll");
                if (File.Exists(p)) return Assembly.LoadFrom(p);
            }
            return null;
        };
        int bad = 0;
        var a = Assembly.LoadFrom(args[1]);
        Console.WriteLine("runtime: " + Environment.Version);
        foreach (var r in a.GetReferencedAssemblies())
        {
            try { var l = Assembly.Load(r); Console.WriteLine("ref ok: {0} -> {1} ({2})", r.Name, l.GetName().Version, l.Location); }
            catch (Exception x) { bad++; Console.WriteLine("ref FAILED: {0}: {1}", r.FullName, x.Message); }
        }
        Type[] types;
        try { types = a.GetTypes(); }
        catch (ReflectionTypeLoadException x)
        {
            foreach (var le in x.LoaderExceptions) Console.WriteLine("type FAILED: " + le.Message);
            return 1;
        }
        foreach (var t in types)
        {
            Console.WriteLine("type: " + t.FullName);
            foreach (var c in t.GetCustomAttributesData()) Console.WriteLine("  attr: " + c);
            foreach (var f in t.GetFields(BindingFlags.Static | BindingFlags.NonPublic | BindingFlags.Public))
                foreach (var c in f.GetCustomAttributesData()) Console.WriteLine("  field " + f.Name + ": " + c);
            foreach (var i in t.GetInterfaces()) Console.WriteLine("  implements: " + i.FullName);
            if (typeof(object).IsAssignableFrom(t) && !t.IsAbstract && t.GetConstructor(Type.EmptyTypes) != null && t.IsPublic)
            {
                try { Activator.CreateInstance(t); Console.WriteLine("  instance ok"); }
                catch (Exception x) { bad++; Console.WriteLine("  instance FAILED: " + x.Message); }
            }
        }
        return bad;
    }
}
