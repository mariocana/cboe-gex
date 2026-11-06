using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Globalization;
using VolSysAPI;
using VolSysAPI.Indicators;
using VolumetricaAPI.Chart;
using VolumetricaControls;
using VolumetricaCore;
using static VolSysAPI.ExternalStructure;
using static VolSysAPI.Structure;

namespace GexIndicators
{
    /// <summary>
    /// Disegna i livelli GEX incollati dal sito nel parametro "Livelli".
    /// Deepchart non ammette file o rete in un indicatore: i dati arrivano solo da qui.
    /// Formato: GEX ES 2026-11-04 08:30|7628.50;tgt long = C3;S|7603.50;gamma flip;N
    /// </summary>
    public class GexLevels : Indicator
    {
        public static IndicatorDescriptionBase Register()
        {
            return new IndicatorDescriptionBase
            {
                Name = "GEX - Livelli",
                Description = "Livelli GEX incollati dal sito.",
                Tags = new List<string> { "GEX", "Opzioni" }
            };
        }

        [Category("Dati")]
        [DisplayName("Livelli (incolla dal sito)")]
        [VolCustom(CategoryIndex = 0, PropertyIndex = 0)]
        public string Livelli { get; set; } = "";

        [Category("Aspetto")]
        [DisplayName("Mostra le etichette")]
        [VolCustom(CategoryIndex = 1, PropertyIndex = 0)]
        public bool Etichette { get; set; } = true;

        [Category("Aspetto")]
        [DisplayName("Colore long")]
        [VolCustom(CategoryIndex = 1, PropertyIndex = 1)]
        public ColorRef ColoreLong { get; set; } = ColorRef.FromRgb(76, 175, 80);

        [Category("Aspetto")]
        [DisplayName("Colore short")]
        [VolCustom(CategoryIndex = 1, PropertyIndex = 2)]
        public ColorRef ColoreShort { get; set; } = ColorRef.FromRgb(224, 85, 85);

        [Category("Aspetto")]
        [DisplayName("Colore flip e gamma massimo")]
        [VolCustom(CategoryIndex = 1, PropertyIndex = 3)]
        public ColorRef ColoreNeutro { get; set; } = ColorRef.FromRgb(240, 192, 64);

        Piano piano;
        readonly List<IAnnotation> linee = new List<IAnnotation>();
        readonly List<IAnnotation> testi = new List<IAnnotation>();
        int ultimaBarra;

        public override void OnSet(bool setDefault, bool themeOverride)
        {
            // Senza almeno una chiamata l'indicatore resta sul grafico ma non disegna
            OnEndCall = CallHandler.HistRT;
            string titolo = Leggi(Livelli).Titolo;
            Description = titolo == "" ? "GEX" : titolo;
        }

        public override void OnLoad()
        {
            piano = Leggi(Livelli);
            linee.Clear();
            testi.Clear();
            ultimaBarra = -1;
            StatusMessage = Avviso(piano, VAn.SymbolName) ?? "GEX: " + piano.Righe.Count + " livelli letti";
        }

        public override void OnEnd(bool isRt)
        {
            int ultima = VAn.LastIndex();
            if (ultima == ultimaBarra)
                return;
            if (ultimaBarra < 0)
                foreach (var livello in piano.Righe)
                    Crea(livello);
            ultimaBarra = ultima;

            // La barra i sta a X = i + 1: linee su tutto il grafico, etichette a destra dell'ultima barra
            foreach (var linea in linee)
            {
                linea.X = 1;
                linea.X2 = ultima + 31;
            }
            foreach (var testo in testi)
                testo.X = ultima + 3;
        }

        private void Crea(Livello livello)
        {
            ColorRef colore = livello.Tipo == "L" ? ColoreLong : livello.Tipo == "S" ? ColoreShort : ColoreNeutro;

            var linea = VAn.CreateAnnotation(AnnotationType.Line);
            linea.Y = livello.Prezzo;
            linea.Y2 = livello.Prezzo;
            linea.LineColor = colore;
            linea.LineWidth = 1;
            VAn.AddAnnotation(IndVars.FrontAnnList, linea);
            linee.Add(linea);

            if (!Etichette)
                return;
            var testo = VAn.CreateAnnotation(AnnotationType.Text);
            testo.Y = livello.Prezzo;
            testo.Text = livello.Nome;
            testo.ForeColor = colore;
            VAn.AddAnnotation(IndVars.FrontAnnList, testo);
            testi.Add(testo);
        }

        public static string Avviso(Piano piano, string simbolo)
        {
            if (piano.Righe.Count == 0)
                return "GEX: incolla i livelli dal sito nelle impostazioni dell'indicatore";
            if (piano.Scartate > 0)
                return "GEX: " + piano.Scartate + " righe non lette, ricopia i livelli dal sito";
            if (piano.Future != "" && !(simbolo ?? "").ToUpperInvariant().Contains(piano.Future))
                return "GEX: livelli " + piano.Future + " su " + simbolo + ", controlla il grafico";
            return null;
        }

        public static Piano Leggi(string testo)
        {
            var piano = new Piano();
            string[] pezzi = (testo ?? "").Split('|');
            for (int i = 0; i < pezzi.Length; i++)
            {
                string pezzo = pezzi[i].Trim();
                if (pezzo == "")
                    continue;
                if (i == 0 && pezzo.StartsWith("GEX "))
                {
                    piano.Titolo = pezzo;
                    string[] parole = pezzo.Split(' ');
                    piano.Future = parole.Length > 1 ? parole[1].ToUpperInvariant() : "";
                    continue;
                }
                string[] campi = pezzo.Split(';');
                if (campi.Length != 3)
                {
                    piano.Scartate++;
                    continue;
                }
                try
                {
                    double prezzo = Convert.ToDouble(campi[0].Trim(), CultureInfo.InvariantCulture);
                    piano.Righe.Add(new Livello { Prezzo = prezzo, Nome = campi[1].Trim(), Tipo = campi[2].Trim() });
                }
                catch (FormatException)
                {
                    piano.Scartate++;
                }
            }
            return piano;
        }
    }

    public class Livello
    {
        public double Prezzo;
        public string Nome = "";
        public string Tipo = "N";
    }

    public class Piano
    {
        public string Titolo = "";
        public string Future = "";
        public int Scartate;
        public List<Livello> Righe = new List<Livello>();
    }
}
