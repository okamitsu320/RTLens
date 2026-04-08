// slang_dump.cpp — RTLens slang elaboration helper
//
// Uses the slang SystemVerilog compiler library (Apache-2.0).
// SPDX-License-Identifier: Apache-2.0 (slang library)
// See: https://github.com/MikePopoloski/slang
//
// This file itself: SPDX-License-Identifier: MIT
// SPDX-FileCopyrightText: RTLens contributors
#include "slang/ast/ASTVisitor.h"
#include "slang/ast/Compilation.h"
#include "slang/ast/Expression.h"
#include "slang/ast/expressions/AssignmentExpressions.h"
#include "slang/ast/expressions/CallExpression.h"
#include "slang/ast/expressions/MiscExpressions.h"
#include "slang/ast/expressions/SelectExpressions.h"
#include "slang/ast/statements/ConditionalStatements.h"
#include "slang/ast/statements/LoopStatements.h"
#include "slang/ast/statements/MiscStatements.h"
#include "slang/ast/symbols/CompilationUnitSymbols.h"
#include "slang/ast/symbols/InstanceSymbols.h"
#include "slang/ast/symbols/MemberSymbols.h"
#include "slang/ast/symbols/PortSymbols.h"
#include "slang/ast/symbols/SubroutineSymbols.h"
#include "slang/driver/Driver.h"
#include "slang/text/SourceManager.h"
#include <algorithm>
#include <cctype>
#include <cstdio>
#include <string>
#include <unordered_set>
#include <vector>

using namespace slang;
using namespace slang::ast;
using namespace slang::driver;

enum class RTLensStage {
    HierScan = 0,
    HierVisit = 1,
    HierDefs = 2,
    Signals = 3,
    Ports = 4,
    Assign = 5,
    Use = 6,
    Callable = 7,
    Full = 8,
};

static std::string lower_copy(const std::string& s) {
    std::string out = s;
    std::transform(out.begin(), out.end(), out.begin(), [](unsigned char c) {
        return static_cast<char>(std::tolower(c));
    });
    return out;
}

static bool parse_rtlens_stage(const std::string& raw, RTLensStage& out) {
    std::string s = lower_copy(raw);
    if (s.empty() || s == "full") {
        out = RTLensStage::Full;
        return true;
    }
    if (s == "hier-scan") {
        out = RTLensStage::HierScan;
        return true;
    }
    if (s == "hier-visit") {
        out = RTLensStage::HierVisit;
        return true;
    }
    if (s == "hier-defs" || s == "hier") {
        out = RTLensStage::HierDefs;
        return true;
    }
    if (s == "signals") {
        out = RTLensStage::Signals;
        return true;
    }
    if (s == "ports") {
        out = RTLensStage::Ports;
        return true;
    }
    if (s == "assign") {
        out = RTLensStage::Assign;
        return true;
    }
    if (s == "use") {
        out = RTLensStage::Use;
        return true;
    }
    if (s == "callable") {
        out = RTLensStage::Callable;
        return true;
    }
    return false;
}

static int stage_rank(RTLensStage s) {
    return static_cast<int>(s);
}

#if defined(SVVIEW_SLANG_ASTVISITOR_FLAGS_API)
template <bool VisitStatements, bool VisitExpressions, bool VisitBad, bool VisitCanonical>
constexpr VisitFlags rtlens_visit_flags() {
    VisitFlags flags = VisitFlags::Symbols;
    if constexpr (VisitStatements) flags = flags | VisitFlags::Statements;
    if constexpr (VisitExpressions) flags = flags | VisitFlags::Expressions;
    if constexpr (VisitBad) flags = flags | VisitFlags::Bad;
    if constexpr (VisitCanonical) flags = flags | VisitFlags::Canonical;
    return flags;
}

template <typename TDerived, bool VisitStatements, bool VisitExpressions, bool VisitBad = false,
          bool VisitCanonical = false>
using SVViewASTVisitor =
    ASTVisitor<TDerived, rtlens_visit_flags<VisitStatements, VisitExpressions, VisitBad,
                                            VisitCanonical>()>;
#else
template <typename TDerived, bool VisitStatements, bool VisitExpressions, bool VisitBad = false,
          bool VisitCanonical = false>
using SVViewASTVisitor = ASTVisitor<TDerived, VisitStatements, VisitExpressions, VisitBad,
                                    VisitCanonical>;
#endif

static std::string esc(const std::string& s) {
    std::string out;
    out.reserve(s.size());
    for (char c : s) {
        if (c == '\\') out += "\\\\";
        else if (c == '\t') out += "\\t";
        else if (c == '\n') out += "\\n";
        else out += c;
    }
    return out;
}

static int line_of(const SourceManager* sm, SourceLocation loc) {
    if (!sm || !loc.valid()) return 0;
    return (int)sm->getLineNumber(loc);
}

static bool is_signal_symbol(const Symbol* s) {
    if (!s) return false;
    return s->kind == SymbolKind::Net || s->kind == SymbolKind::Variable;
}

template <typename SymT>
static const Expression* maybe_initializer(const SymT& sym) {
    if constexpr (requires { sym.getInitializer(); })
        return sym.getInitializer();
    else
        return nullptr;
}

class SymbolCollector : public SVViewASTVisitor<SymbolCollector, false, true> {
public:
    SymbolCollector(std::vector<const Symbol*>& out, bool lhsMode) : out_(out), lhsMode_(lhsMode) {}

    void handle(const NamedValueExpression& nve) {
        if (lhsMode_) {
            if (selectorDepth_ == 0)
                out_.push_back(&nve.symbol);
        }
        else {
            out_.push_back(&nve.symbol);
        }
    }

    void handle(const RangeSelectExpression& expr) {
        expr.value().visit(*this);
        if (!lhsMode_) {
            selectorDepth_++;
            expr.left().visit(*this);
            expr.right().visit(*this);
            selectorDepth_--;
        }
    }

    void handle(const ElementSelectExpression& expr) {
        expr.value().visit(*this);
        if (!lhsMode_) {
            selectorDepth_++;
            expr.selector().visit(*this);
            selectorDepth_--;
        }
    }

private:
    std::vector<const Symbol*>& out_;
    bool lhsMode_;
    int selectorDepth_ = 0;
};

static void emit_decl_initializer_edges(const SourceManager* sm, const Symbol& lhsSym,
                                        const Expression* initExpr, SourceLocation fallbackLoc) {
    if (!sm || !initExpr) return;
    std::string lhs = std::string(lhsSym.getHierarchicalPath());
    if (lhs.empty()) return;

    SourceLocation loc = initExpr->sourceRange.start();
    if (!loc.valid()) loc = fallbackLoc;
    std::string f = std::string(sm->getFileName(loc));
    int l = line_of(sm, loc);
    std::printf("D\t%s\t%s\t%d\n", esc(lhs).c_str(), esc(f).c_str(), l);

    std::vector<const Symbol*> rhs;
    SymbolCollector rhsCollector(rhs, false);
    initExpr->visit(rhsCollector);
    for (const Symbol* r : rhs) {
        if (!is_signal_symbol(r)) continue;
        std::string src = std::string(r->getHierarchicalPath());
        if (src.empty()) continue;
        std::printf("LD\t%s\t%s\t%d\n", esc(src).c_str(), esc(f).c_str(), l);
        std::printf("ED\t%s\t%s\t%s\t%d\n", esc(src).c_str(), esc(lhs).c_str(), esc(f).c_str(), l);
    }
}

class NamedValueExtractor : public SVViewASTVisitor<NamedValueExtractor, false, true> {
public:
    explicit NamedValueExtractor(std::vector<const Symbol*>& out) : out_(out) {}
    void handle(const NamedValueExpression& nve) { out_.push_back(&nve.symbol); }

private:
    std::vector<const Symbol*>& out_;
};

struct SymbolWithLoc {
    const Symbol* sym;
    SourceLocation loc;
};

class SymbolLocCollector : public SVViewASTVisitor<SymbolLocCollector, false, true> {
public:
    explicit SymbolLocCollector(std::vector<SymbolWithLoc>& out) : out_(out) {}

    void handle(const NamedValueExpression& nve) { out_.push_back({&nve.symbol, nve.sourceRange.start()}); }

    void handle(const RangeSelectExpression& expr) {
        expr.value().visit(*this);
        selectorDepth_++;
        expr.left().visit(*this);
        expr.right().visit(*this);
        selectorDepth_--;
    }

    void handle(const ElementSelectExpression& expr) {
        expr.value().visit(*this);
        selectorDepth_++;
        expr.selector().visit(*this);
        selectorDepth_--;
    }

private:
    std::vector<SymbolWithLoc>& out_;
    int selectorDepth_ = 0;
};

class AssignmentEdgeEmitter : public SVViewASTVisitor<AssignmentEdgeEmitter, true, true> {
public:
    AssignmentEdgeEmitter(const SourceManager* sm) : sm_(sm) {}

    void handle(const AssignmentExpression& assignment) {
        std::vector<const Symbol*> lhs;
        std::vector<const Symbol*> rhs;
        SymbolCollector lhsCollector(lhs, true);
        SymbolCollector rhsCollector(rhs, false);
        assignment.left().visit(lhsCollector);
        assignment.right().visit(rhsCollector);

        SourceLocation loc = assignment.sourceRange.start();
        std::string f = std::string(sm_->getFileName(loc));
        int l = line_of(sm_, loc);
        for (const Symbol* lSym : lhs) {
            if (lSym == nullptr || !is_signal_symbol(lSym)) continue;
            std::string s = std::string(lSym->getHierarchicalPath());
            if (s.empty()) continue;
            std::printf("D\t%s\t%s\t%d\n", esc(s).c_str(), esc(f).c_str(), l);
        }
        for (const Symbol* r : rhs) {
            for (const Symbol* lSym : lhs) {
                if (r == nullptr || lSym == nullptr) continue;
                if (!is_signal_symbol(r) || !is_signal_symbol(lSym)) continue;
                std::string src = std::string(r->getHierarchicalPath());
                std::string dst = std::string(lSym->getHierarchicalPath());
                if (src.empty() || dst.empty()) continue;
                std::printf("ED\t%s\t%s\t%s\t%d\n", esc(src).c_str(), esc(dst).c_str(), esc(f).c_str(),
                            l);
            }
        }

        for (const SymbolWithLoc& dep : controlDeps_) {
            const Symbol* cSym = dep.sym;
            SourceLocation cLoc = dep.loc;
            std::string cf = std::string(sm_->getFileName(cLoc));
            int cl = line_of(sm_, cLoc);
            for (const Symbol* lSym : lhs) {
                if (cSym == nullptr || lSym == nullptr) continue;
                if (!is_signal_symbol(cSym) || !is_signal_symbol(lSym)) continue;
                std::string src = std::string(cSym->getHierarchicalPath());
                std::string dst = std::string(lSym->getHierarchicalPath());
                if (src.empty() || dst.empty()) continue;
                std::printf("EC\t%s\t%s\t%s\t%d\n", esc(src).c_str(), esc(dst).c_str(), esc(cf).c_str(),
                            cl);
            }
        }
    }

    void handle(const ConditionalStatement& stmt) {
        size_t prevSize = controlDeps_.size();
        SymbolLocCollector c(controlDeps_);
        for (auto& cond : stmt.conditions) {
            cond.expr->visit(c);
        }
        stmt.ifTrue.visit(*this);
        if (stmt.ifFalse) stmt.ifFalse->visit(*this);
        controlDeps_.resize(prevSize);
    }

    void handle(const CaseStatement& stmt) {
        size_t prevSize = controlDeps_.size();
        SymbolLocCollector c(controlDeps_);
        stmt.expr.visit(c);
        for (auto& item : stmt.items) {
            for (auto itemExpr : item.expressions) {
                itemExpr->visit(c);
            }
            item.stmt->visit(*this);
        }
        if (stmt.defaultCase) stmt.defaultCase->visit(*this);
        controlDeps_.resize(prevSize);
    }

    void handle(const PatternCaseStatement& stmt) {
        size_t prevSize = controlDeps_.size();
        SymbolLocCollector c(controlDeps_);
        stmt.expr.visit(c);
        for (auto& item : stmt.items) {
            if (item.filter) item.filter->visit(c);
        }
        for (auto& item : stmt.items) {
            item.stmt->visit(*this);
        }
        if (stmt.defaultCase) stmt.defaultCase->visit(*this);
        controlDeps_.resize(prevSize);
    }

    void handle(const ForLoopStatement& stmt) {
        for (auto init : stmt.initializers) {
            init->visit(*this);
        }
        size_t prevSize = controlDeps_.size();
        SymbolLocCollector c(controlDeps_);
        if (stmt.stopExpr) stmt.stopExpr->visit(c);
        stmt.body.visit(*this);
        controlDeps_.resize(prevSize);
        for (auto step : stmt.steps) {
            step->visit(*this);
        }
    }

    void handle(const WhileLoopStatement& stmt) {
        size_t prevSize = controlDeps_.size();
        SymbolLocCollector c(controlDeps_);
        stmt.cond.visit(c);
        stmt.body.visit(*this);
        controlDeps_.resize(prevSize);
    }

    void handle(const DoWhileLoopStatement& stmt) {
        size_t prevSize = controlDeps_.size();
        SymbolLocCollector c(controlDeps_);
        stmt.cond.visit(c);
        stmt.body.visit(*this);
        controlDeps_.resize(prevSize);
    }

    void handle(const RepeatLoopStatement& stmt) {
        size_t prevSize = controlDeps_.size();
        SymbolLocCollector c(controlDeps_);
        stmt.count.visit(c);
        stmt.body.visit(*this);
        controlDeps_.resize(prevSize);
    }

    void handle(const ForeachLoopStatement& stmt) {
        size_t prevSize = controlDeps_.size();
        SymbolLocCollector c(controlDeps_);
        stmt.arrayRef.visit(c);
        stmt.body.visit(*this);
        controlDeps_.resize(prevSize);
    }

    void handle(const WaitStatement& stmt) {
        size_t prevSize = controlDeps_.size();
        SymbolLocCollector c(controlDeps_);
        stmt.cond.visit(c);
        stmt.stmt.visit(*this);
        controlDeps_.resize(prevSize);
    }

    void handle(const RandCaseStatement& stmt) {
        size_t prevSize = controlDeps_.size();
        SymbolLocCollector c(controlDeps_);
        for (auto& item : stmt.items) {
            item.expr->visit(c);
        }
        for (auto& item : stmt.items) {
            item.stmt->visit(*this);
        }
        controlDeps_.resize(prevSize);
    }

    void handle(const InstanceSymbol&) {}
    void handle(const UninstantiatedDefSymbol&) {}

private:
    const SourceManager* sm_;
    std::vector<SymbolWithLoc> controlDeps_;
};

class UseSiteEmitter : public SVViewASTVisitor<UseSiteEmitter, true, true> {
public:
    UseSiteEmitter(const SourceManager* sm) : sm_(sm) {}

    void handle(const AssignmentExpression& assignment) {
        bool prev = inLhs_;
        inLhs_ = true;
        assignment.left().visit(*this);
        inLhs_ = false;
        assignment.right().visit(*this);
        inLhs_ = prev;
    }

    void handle(const ConditionalStatement& stmt) {
        bool prevCtrl = inControl_;
        inControl_ = true;
        for (auto& cond : stmt.conditions) {
            cond.expr->visit(*this);
        }
        inControl_ = prevCtrl;
        stmt.ifTrue.visit(*this);
        if (stmt.ifFalse) stmt.ifFalse->visit(*this);
    }

    void handle(const CaseStatement& stmt) {
        bool prevCtrl = inControl_;
        inControl_ = true;
        stmt.expr.visit(*this);
        for (auto& item : stmt.items) {
            for (auto itemExpr : item.expressions) {
                itemExpr->visit(*this);
            }
        }
        inControl_ = prevCtrl;
        for (auto& item : stmt.items) {
            item.stmt->visit(*this);
        }
        if (stmt.defaultCase) stmt.defaultCase->visit(*this);
    }

    void handle(const PatternCaseStatement& stmt) {
        bool prevCtrl = inControl_;
        inControl_ = true;
        stmt.expr.visit(*this);
        for (auto& item : stmt.items) {
            if (item.filter) item.filter->visit(*this);
        }
        inControl_ = prevCtrl;
        for (auto& item : stmt.items) {
            item.stmt->visit(*this);
        }
        if (stmt.defaultCase) stmt.defaultCase->visit(*this);
    }

    void handle(const ForLoopStatement& stmt) {
        bool prevCtrl = inControl_;
        inControl_ = prevCtrl;
        for (auto init : stmt.initializers) {
            init->visit(*this);
        }
        inControl_ = true;
        if (stmt.stopExpr) stmt.stopExpr->visit(*this);
        inControl_ = prevCtrl;
        stmt.body.visit(*this);
        for (auto step : stmt.steps) {
            step->visit(*this);
        }
    }

    void handle(const WhileLoopStatement& stmt) {
        bool prevCtrl = inControl_;
        inControl_ = true;
        stmt.cond.visit(*this);
        inControl_ = prevCtrl;
        stmt.body.visit(*this);
    }

    void handle(const DoWhileLoopStatement& stmt) {
        bool prevCtrl = inControl_;
        inControl_ = true;
        stmt.cond.visit(*this);
        inControl_ = prevCtrl;
        stmt.body.visit(*this);
    }

    void handle(const RepeatLoopStatement& stmt) {
        bool prevCtrl = inControl_;
        inControl_ = true;
        stmt.count.visit(*this);
        inControl_ = prevCtrl;
        stmt.body.visit(*this);
    }

    void handle(const ForeachLoopStatement& stmt) {
        bool prevCtrl = inControl_;
        inControl_ = true;
        stmt.arrayRef.visit(*this);
        inControl_ = prevCtrl;
        stmt.body.visit(*this);
    }

    void handle(const WaitStatement& stmt) {
        bool prevCtrl = inControl_;
        inControl_ = true;
        stmt.cond.visit(*this);
        inControl_ = prevCtrl;
        stmt.stmt.visit(*this);
    }

    void handle(const RandCaseStatement& stmt) {
        bool prevCtrl = inControl_;
        inControl_ = true;
        for (auto& item : stmt.items) {
            item.expr->visit(*this);
        }
        inControl_ = prevCtrl;
        for (auto& item : stmt.items) {
            item.stmt->visit(*this);
        }
    }

    void handle(const NamedValueExpression& nve) {
        if (inLhs_) return;
        const Symbol* s = &nve.symbol;
        if (!is_signal_symbol(s)) return;
        std::string sig = std::string(s->getHierarchicalPath());
        if (sig.empty()) return;
        SourceLocation loc = nve.sourceRange.start();
        std::string f = std::string(sm_->getFileName(loc));
        int l = line_of(sm_, loc);
        if (inControl_)
            std::printf("LC\t%s\t%s\t%d\n", esc(sig).c_str(), esc(f).c_str(), l);
        else
            std::printf("LD\t%s\t%s\t%d\n", esc(sig).c_str(), esc(f).c_str(), l);
    }

private:
    const SourceManager* sm_;
    bool inLhs_ = false;
    bool inControl_ = false;
};

class CallableRefEmitter : public SVViewASTVisitor<CallableRefEmitter, true, true> {
public:
    explicit CallableRefEmitter(const SourceManager* sm) : sm_(sm) {}

    void handle(const CallExpression& expr) {
        if (!std::holds_alternative<const SubroutineSymbol*>(expr.subroutine)) return;
        const SubroutineSymbol* sub = std::get<const SubroutineSymbol*>(expr.subroutine);
        if (!sub) return;
        std::string kind = sub->subroutineKind == SubroutineKind::Task ? "task" : "function";
        std::string full = std::string(sub->getHierarchicalPath());
        std::string name = std::string(sub->name);
        if (full.empty() || name.empty()) return;
        SourceLocation loc = expr.sourceRange.start();
        std::string f = std::string(sm_->getFileName(loc));
        int l = line_of(sm_, loc);
        std::printf("SR\t%s\t%s\t%s\t%s\t%d\n", esc(kind).c_str(), esc(full).c_str(), esc(name).c_str(),
                    esc(f).c_str(), l);
    }

private:
    const SourceManager* sm_;
};

int main(int argc, char** argv) {
    Driver driver;
    driver.addStandardArgs();

    std::optional<std::string> topName;
    driver.cmdLine.add("--rtlens-top", topName, "Explicit top instance name", "<name>");
    std::optional<std::string> stageName;
    driver.cmdLine.add("--rtlens-stage", stageName,
                       "Stage selector: hier-scan|hier-visit|hier-defs|signals|ports|assign|use|callable|full",
                       "<stage>");

    if (!driver.parseCommandLine(argc, argv)) return 1;
    if (!driver.processOptions()) return 2;
    if (!driver.parseAllSources()) return 3;

    RTLensStage stage = RTLensStage::Full;
    if (stageName.has_value() && !parse_rtlens_stage(stageName.value(), stage)) {
        std::fprintf(stderr,
                     "invalid --rtlens-stage: %s (expected hier-scan|hier-visit|hier-defs|signals|ports|assign|use|callable|full)\n",
                     stageName.value().c_str());
        return 5;
    }
    const bool traverseInstances = stage_rank(stage) >= stage_rank(RTLensStage::HierVisit);
    const bool emitModuleDefs = stage_rank(stage) >= stage_rank(RTLensStage::HierDefs);
    const bool emitSignals = stage_rank(stage) >= stage_rank(RTLensStage::Signals);
    const bool emitPorts = stage_rank(stage) >= stage_rank(RTLensStage::Ports);
    const bool emitAssign = stage_rank(stage) >= stage_rank(RTLensStage::Assign);
    const bool emitUse = stage_rank(stage) >= stage_rank(RTLensStage::Use);
    const bool emitCallable = stage_rank(stage) >= stage_rank(RTLensStage::Callable);

    auto compilation = driver.createCompilation();
    const SourceManager* sm = compilation->getSourceManager();
    auto& root = compilation->getRoot();
    std::unordered_set<std::string> seenModuleDefs;
    std::unordered_set<std::string> seenSubDefs;

    std::vector<const InstanceSymbol*> tops;
    for (const InstanceSymbol* t : root.topInstances) {
        if (!topName.has_value() || t->name == topName.value()) tops.push_back(t);
    }
    if (tops.empty()) {
        for (const InstanceSymbol* t : root.topInstances) tops.push_back(t);
    }

    for (const InstanceSymbol* t : tops) {
        if (!traverseInstances) continue;
        t->visit(makeVisitor([&](auto& visitor, const InstanceSymbol& inst) {
            const auto& def = inst.getDefinition();
            std::string instPath = std::string(inst.getHierarchicalPath());
            std::string modName = std::string(def.name);
            std::string file = std::string(sm->getFileName(def.location));
            int line = line_of(sm, def.location);
            std::printf("H\t%s\t%s\t%s\t%d\n", esc(instPath).c_str(), esc(modName).c_str(),
                        esc(file).c_str(), line);

            std::string mkey = "module:" + modName;
            if (emitModuleDefs && seenModuleDefs.emplace(mkey).second) {
                std::printf("MD\tmodule\t%s\t%s\t%d\n", esc(modName).c_str(), esc(file).c_str(), line);
            }

            if (emitSignals) {
                for (const auto& net : inst.body.membersOfType<NetSymbol>()) {
                    std::string p = std::string(net.getHierarchicalPath());
                    std::string f = std::string(sm->getFileName(net.location));
                    int l = line_of(sm, net.location);
                    std::printf("S\t%s\tnet\t%s\t%d\n", esc(p).c_str(), esc(f).c_str(), l);
                    if (const Expression* init = maybe_initializer(net))
                        emit_decl_initializer_edges(sm, net, init, net.location);
                }
                for (const auto& var : inst.body.membersOfType<VariableSymbol>()) {
                    std::string p = std::string(var.getHierarchicalPath());
                    std::string f = std::string(sm->getFileName(var.location));
                    int l = line_of(sm, var.location);
                    std::printf("S\t%s\tvar\t%s\t%d\n", esc(p).c_str(), esc(f).c_str(), l);
                    if (const Expression* init = maybe_initializer(var))
                        emit_decl_initializer_edges(sm, var, init, var.location);
                }
            }

            if (emitPorts) {
                for (const PortConnection* conn : inst.getPortConnections()) {
                    const PortSymbol* port = conn->port.as_if<PortSymbol>();
                    const Expression* expr = conn->getExpression();
                    if (!port || !expr) continue;

                    std::vector<const Symbol*> syms;
                    NamedValueExtractor nv(syms);
                    expr->visit(nv);
                    if (syms.empty()) continue;
                    std::string childPort = instPath + "." + std::string(port->name);
                    SourceLocation loc = expr->sourceRange.start();
                    std::string f = std::string(sm->getFileName(loc));
                    int l = line_of(sm, loc);

                    for (const Symbol* sym : syms) {
                        std::string parentSig = std::string(sym->getHierarchicalPath());
                        if (parentSig.empty()) continue;
                        switch (port->direction) {
                            case ArgumentDirection::In:
                                std::printf("DP\t%s\t%s\t%d\n", esc(childPort).c_str(), esc(f).c_str(), l);
                                std::printf("LP\t%s\t%s\t%d\n", esc(parentSig).c_str(), esc(f).c_str(), l);
                                std::printf("EP\t%s\t%s\t%s\t%d\n", esc(parentSig).c_str(),
                                            esc(childPort).c_str(), esc(f).c_str(), l);
                                break;
                            case ArgumentDirection::Out:
                                std::printf("DP\t%s\t%s\t%d\n", esc(parentSig).c_str(), esc(f).c_str(), l);
                                std::printf("LP\t%s\t%s\t%d\n", esc(childPort).c_str(), esc(f).c_str(), l);
                                std::printf("EP\t%s\t%s\t%s\t%d\n", esc(childPort).c_str(),
                                            esc(parentSig).c_str(), esc(f).c_str(), l);
                                break;
                            default:
                                std::printf("DP\t%s\t%s\t%d\n", esc(childPort).c_str(), esc(f).c_str(), l);
                                std::printf("DP\t%s\t%s\t%d\n", esc(parentSig).c_str(), esc(f).c_str(), l);
                                std::printf("LP\t%s\t%s\t%d\n", esc(parentSig).c_str(), esc(f).c_str(), l);
                                std::printf("LP\t%s\t%s\t%d\n", esc(childPort).c_str(), esc(f).c_str(), l);
                                std::printf("EP\t%s\t%s\t%s\t%d\n", esc(parentSig).c_str(),
                                            esc(childPort).c_str(), esc(f).c_str(), l);
                                std::printf("EP\t%s\t%s\t%s\t%d\n", esc(childPort).c_str(),
                                            esc(parentSig).c_str(), esc(f).c_str(), l);
                                break;
                        }
                    }
                }

                for (const auto& childInst : inst.body.membersOfType<InstanceSymbol>()) {
                    std::string targetMod = std::string(childInst.getDefinition().name);
                    std::string instName = std::string(childInst.name);
                    std::string rf = std::string(sm->getFileName(childInst.location));
                    int rl = line_of(sm, childInst.location);
                    std::printf("MR\tmodule\t%s\t%s\t%s\t%d\n", esc(targetMod).c_str(), esc(instName).c_str(),
                                esc(rf).c_str(), rl);
                }
            }

            if (emitCallable) {
                for (const auto& sub : inst.body.membersOfType<SubroutineSymbol>()) {
                    std::string skind = sub.subroutineKind == SubroutineKind::Task ? "task" : "function";
                    std::string full = std::string(sub.getHierarchicalPath());
                    std::string name = std::string(sub.name);
                    if (full.empty() || name.empty()) continue;
                    std::string sf = std::string(sm->getFileName(sub.location));
                    int sl = line_of(sm, sub.location);
                    std::string skey = skind + ":" + full;
                    if (seenSubDefs.emplace(skey).second) {
                        std::printf("SD\t%s\t%s\t%s\t%s\t%d\n", esc(skind).c_str(), esc(full).c_str(),
                                    esc(name).c_str(), esc(sf).c_str(), sl);
                    }
                }
            }

            if (emitAssign) {
                AssignmentEdgeEmitter edgeEmitter(sm);
                inst.body.visit(edgeEmitter);
            }
            if (emitUse) {
                UseSiteEmitter useEmitter(sm);
                inst.body.visit(useEmitter);
            }
            if (emitCallable) {
                CallableRefEmitter cref(sm);
                inst.body.visit(cref);
            }

            visitor.visitDefault(inst);
        }));
    }

    driver.reportCompilation(*compilation, true);
    bool ok = driver.reportDiagnostics(true);
    return ok ? 0 : 4;
}
