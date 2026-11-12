// Visual Studio client for the KC language server (editors/kclsp): a MEF ILanguageClient for the "kc" content type.
// Built by editors/vs/build_vsix.py with the csc and the assemblies of the Visual Studio installation.
//
// Which Python and which x3kc checkout: environment variables KCLSP_PYTHON / X3KC_ROOT if set, else kclsp.json next
// to this DLL (written by build_vsix.py: the Python that ran the build and the checkout it was run from).
using System;
using System.Collections.Generic;
using System.ComponentModel.Composition;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Runtime.Serialization;
using System.Runtime.Serialization.Json;
using System.Threading;
using System.Threading.Tasks;
using Microsoft.VisualStudio.LanguageServer.Client;
using Microsoft.VisualStudio.Threading;
using Microsoft.VisualStudio.Utilities;

namespace X3kc.Kc
{
    public static class KcContentType
    {
        public const string Name = "kc";

        [Export]
        [Name(Name)]
        [BaseDefinition(CodeRemoteContentDefinition.CodeRemoteContentTypeName)]
        internal static ContentTypeDefinition Definition;

        [Export]
        [FileExtension(".kc")]
        [ContentType(Name)]
        internal static FileExtensionToContentTypeDefinition KcFiles;
    }

    [DataContract]
    internal class KcSettings
    {
        [DataMember(Name = "python")] public string Python;
        [DataMember(Name = "x3kc_root")] public string Root;
        [DataMember(Name = "log")] public string Log;

        public static KcSettings Load()
        {
            var s = new KcSettings();
            try
            {
                var dir = Path.GetDirectoryName(Assembly.GetExecutingAssembly().Location);
                var p = Path.Combine(dir, "kclsp.json");
                if (File.Exists(p))
                    using (var f = File.OpenRead(p))
                        s = (KcSettings)new DataContractJsonSerializer(typeof(KcSettings)).ReadObject(f);
            }
            catch (Exception)
            {
            }
            s.Python = Environment.GetEnvironmentVariable("KCLSP_PYTHON") ?? s.Python ?? "python";
            s.Root = Environment.GetEnvironmentVariable("X3KC_ROOT") ?? s.Root;
            s.Log = Environment.GetEnvironmentVariable("KCLSP_LOG") ?? s.Log;
            return s;
        }
    }

    [ContentType(KcContentType.Name)]
    [Export(typeof(ILanguageClient))]
    public class KcLanguageClient : ILanguageClient
    {
        private Process server;

        public string Name => "KC (x3kc)";
        public IEnumerable<string> ConfigurationSections => null;
        public object InitializationOptions => null;
        public IEnumerable<string> FilesToWatch => null;
        public bool ShowNotificationOnInitializeFailed => true;
        public event AsyncEventHandler<EventArgs> StartAsync;
        public event AsyncEventHandler<EventArgs> StopAsync;

        public async Task<Connection> ActivateAsync(CancellationToken token)
        {
            await Task.Yield();
            var s = KcSettings.Load();
            var info = new ProcessStartInfo(s.Python, "-X utf8 -B -m editors.kclsp")
            {
                RedirectStandardInput = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                UseShellExecute = false,
                CreateNoWindow = true,
            };
            if (!string.IsNullOrEmpty(s.Root))
            {
                info.WorkingDirectory = s.Root;
                info.EnvironmentVariables["PYTHONPATH"] = s.Root;
            }
            if (!string.IsNullOrEmpty(s.Log))
                info.EnvironmentVariables["KCLSP_LOG"] = s.Log;
            server = Process.Start(info);
            server.ErrorDataReceived += (o, e) => { };      // drained so a chatty server cannot block on stderr
            server.BeginErrorReadLine();
            return new Connection(server.StandardOutput.BaseStream, server.StandardInput.BaseStream);
        }

        public async Task OnLoadedAsync()
        {
            if (StartAsync != null)
                await StartAsync.InvokeAsync(this, EventArgs.Empty);
        }

        public Task OnServerInitializedAsync() => Task.CompletedTask;

        public Task<InitializationFailureContext> OnServerInitializeFailedAsync(ILanguageClientInitializationInfo initializationState)
        {
            var s = KcSettings.Load();
            return Task.FromResult(new InitializationFailureContext
            {
                FailureMessage = string.Format("KC language server failed to start ({0} -m editors.kclsp in {1}): {2}",
                    s.Python, s.Root ?? "(no X3KC_ROOT)", initializationState.StatusMessage),
            });
        }
    }
}
